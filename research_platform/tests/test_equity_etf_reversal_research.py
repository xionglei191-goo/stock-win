from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from research_platform.equity_etf_reversal_research import (
    EquityEtfAsset,
    assess_development,
    build_cross_sectional_reversal_events,
    create_development_snapshot,
    decode_tnf_security_master,
    is_domestic_equity_etf,
    load_development_snapshot,
    protocol_manifest,
)
from research_platform.etf_pullback_research import DAY_DTYPE


class EquityEtfReversalResearchTests(unittest.TestCase):
    def test_tnf_decoder_and_asset_classification(self) -> None:
        record = bytearray(360)
        record[:6] = b"510001"
        name = "Industry ETF".encode("gbk")
        record[31 : 31 + len(name)] = name
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "shs.tnf"
            path.write_bytes(bytes(50) + bytes(record))
            master = decode_tnf_security_master(path, "sh")
        self.assertEqual(master.to_dict("records"), [{"code": "510001.SH", "name": "Industry ETF"}])
        self.assertTrue(is_domestic_equity_etf("510001.SH", "Industry ETF"))
        self.assertFalse(is_domestic_equity_etf("511010.SH", "Bond ETF"))
        self.assertFalse(is_domestic_equity_etf("160119.SZ", "500 ETF feeder LOF"))
        self.assertFalse(is_domestic_equity_etf("159003.SZ", "Cash ETF"))

    def test_snapshot_excludes_future_windows_and_verifies_hash(self) -> None:
        asset = EquityEtfAsset("510001.SH", "Industry ETF", "sh", "sh510001")
        records = np.zeros(3, dtype=DAY_DTYPE)
        records["date"] = [20240102, 20240628, 20240701]
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
            manifest = create_development_snapshot(
                tdx_root=root / "tdx",
                output_root=root / "snapshots",
                assets=[asset],
            )
            snapshot_dir = root / "snapshots" / manifest["snapshot_id"]
            bars = load_development_snapshot(snapshot_dir)
            self.assertEqual(len(bars), 2)
            self.assertEqual(str(bars["timestamp"].max().date()), "2024-06-28")
            path = snapshot_dir / "bars.parquet"
            path.write_bytes(path.read_bytes() + b"tampered")
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                load_development_snapshot(snapshot_dir)

    def test_correlated_duplicate_is_removed_and_entry_uses_next_open(self) -> None:
        bars, market, dates = synthetic_market()
        events = build_cross_sectional_reversal_events(bars, market)
        self.assertEqual(len(events), 2)
        self.assertEqual(int(events["selected"].sum()), 1)
        self.assertEqual(int(events["blocked_correlation"].sum()), 1)
        selected = events.loc[events["selected"]].iloc[0]
        signal_position = dates.get_loc(pd.Timestamp(selected["signal_date"]))
        self.assertEqual(pd.Timestamp(selected["entry_date"]), dates[signal_position + 1])
        self.assertTrue(bool(selected["executable"]))
        self.assertGreaterEqual(int(selected["holding_sessions"]), 1)
        self.assertGreater(int(selected["quantity"]), 0)

    def test_entry_gap_over_three_percent_cancels_order(self) -> None:
        bars, market, dates = synthetic_market()
        baseline = build_cross_sectional_reversal_events(bars, market)
        selected = baseline.loc[baseline["selected"]].iloc[0]
        signal_position = dates.get_loc(pd.Timestamp(selected["signal_date"]))
        entry_date = dates[signal_position + 1]
        changed = bars.copy()
        mask = changed["code"].eq(selected["code"]) & changed["timestamp"].eq(entry_date)
        signal_close = float(
            changed.loc[
                changed["code"].eq(selected["code"])
                & changed["timestamp"].eq(pd.Timestamp(selected["signal_date"])),
                "Close",
            ].iloc[0]
        )
        changed.loc[mask, "Open"] = signal_close * 1.04
        events = build_cross_sectional_reversal_events(changed, market)
        blocked = events.loc[events["selected"]].iloc[0]
        self.assertTrue(bool(blocked["blocked_entry_gap"]))
        self.assertFalse(bool(blocked["executable"]))

    def test_appended_future_does_not_change_historical_signal_selection(self) -> None:
        bars, market, dates = synthetic_market()
        baseline = build_cross_sectional_reversal_events(bars, market)
        future_dates = pd.bdate_range(dates[-1] + pd.Timedelta(days=1), periods=5)
        future_frames = []
        for code, group in bars.groupby("code", sort=False):
            last = group.iloc[-1]
            future_frames.append(
                pd.DataFrame(
                    {
                        "code": code,
                        "name": last["name"],
                        "timestamp": future_dates,
                        "Open": [float(last["Close"]) * 0.5] * 5,
                        "High": [float(last["Close"]) * 0.6] * 5,
                        "Low": [float(last["Close"]) * 0.4] * 5,
                        "Close": [float(last["Close"]) * 0.5] * 5,
                        "Amount": [500_000_000.0] * 5,
                        "Volume": [50_000_000.0] * 5,
                    }
                )
            )
        future_market = pd.concat(
            [
                market,
                pd.DataFrame(
                    {
                        "timestamp": future_dates,
                        "Close": [float(market["Close"].iloc[-1]) * 0.8] * 5,
                    }
                ),
            ],
            ignore_index=True,
        )
        changed = build_cross_sectional_reversal_events(
            pd.concat([bars, *future_frames], ignore_index=True),
            future_market,
        )
        columns = [
            "code",
            "signal_date",
            "score",
            "daily_rank",
            "selected",
            "blocked_correlation",
            "blocked_daily_capacity",
        ]
        cutoff = dates[-1]
        pd.testing.assert_frame_equal(
            baseline.loc[baseline["signal_date"].le(cutoff), columns].reset_index(drop=True),
            changed.loc[changed["signal_date"].le(cutoff), columns].reset_index(drop=True),
        )

    def test_passing_development_still_requires_survivor_audit(self) -> None:
        reports = []
        for label in ("dev_2021_2022", "dev_2022_2023", "dev_2023_2024"):
            reports.append(
                {
                    "window": {"label": label, "role": "DEVELOPMENT"},
                    "portfolio_trades": 40,
                    "portfolio_annualized_return": 0.10,
                    "portfolio_total_return": 0.10,
                    "median_trade_return": 0.01,
                    "ex_top3_contribution": 0.02,
                    "portfolio_max_drawdown": -0.05,
                    "fill_rate": 0.90,
                }
            )
        decision = assess_development(reports)
        self.assertEqual(decision["decision"], "REQUIRE_SURVIVOR_AUDIT")
        self.assertTrue(decision["survivor_audit_required"])
        self.assertFalse(decision["replication_opened"])
        self.assertFalse(decision["holdout_opened"])
        self.assertEqual(
            protocol_manifest()["opening_rule"],
            "development, survivor audit, replication, then holdout",
        )


def synthetic_market() -> tuple[pd.DataFrame, pd.DataFrame, pd.DatetimeIndex]:
    sessions = 340
    dates = pd.bdate_range("2020-01-01", periods=sessions)
    frames = []
    for index in range(10):
        code = f"510{index + 1:03d}.SH"
        close = np.linspace(10.0, 17.0 if index < 2 else 12.0 - index * 0.05, sessions)
        if index < 2:
            for position, daily_return in ((317, -0.01), (318, -0.015), (319, -0.02), (320, -0.02)):
                close[position] = close[position - 1] * (1.0 + daily_return)
            close[321] = close[320] * 1.005
            close[322] = close[321] * 1.03
            close[323] = close[322] * 1.03
            close[324:] = close[323] + np.arange(sessions - 324) * 0.02
        open_price = close.copy()
        open_price[321] = close[320]
        frames.append(
            pd.DataFrame(
                {
                    "code": code,
                    "name": f"ETF {index}",
                    "timestamp": dates,
                    "Open": open_price,
                    "High": close * 1.01,
                    "Low": close * 0.99,
                    "Close": close,
                    "Amount": 100_000_000.0 + index * 10_000_000.0,
                    "Volume": 10_000_000.0,
                }
            )
        )
    market_close = np.linspace(3000.0, 4000.0, sessions)
    market = pd.DataFrame({"timestamp": dates, "Close": market_close})
    return pd.concat(frames, ignore_index=True), market, dates


if __name__ == "__main__":
    unittest.main()
