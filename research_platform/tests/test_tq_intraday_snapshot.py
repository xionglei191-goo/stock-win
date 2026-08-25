from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from research_platform.__main__ import build_parser
from research_platform.tq_intraday_snapshot import (
    REQUIRED_FIELDS,
    TQDataError,
    build_event_acquisition_watchlist,
    capture_tq_intraday_snapshot,
    capture_tq_watchlist_file,
    fetch_tq_intraday_batches,
    normalize_tq_market_data,
    plan_tq_intraday_batches,
    plan_tq_watchlist_batches,
    validate_tq_intraday_bars,
    write_immutable_tq_snapshot,
    write_immutable_tq_watchlist,
)


def _session_index(day: str) -> pd.DatetimeIndex:
    date = pd.Timestamp(day)
    morning = pd.date_range(date.replace(hour=9, minute=35), date.replace(hour=11, minute=30), freq="5min")
    afternoon = pd.date_range(date.replace(hour=13, minute=5), date.replace(hour=15), freq="5min")
    return morning.append(afternoon)


def _response(codes: list[str], day: str) -> dict[str, object]:
    index = _session_index(day)
    fields: dict[str, object] = {"ErrorId": "0"}
    for field in REQUIRED_FIELDS:
        values: dict[str, list[float]] = {}
        for offset, code in enumerate(codes):
            close = pd.Series(range(len(index)), dtype=float).mul(0.01).add(10.0 + offset)
            if field == "Open":
                series = close - 0.01
            elif field == "High":
                series = close + 0.02
            elif field == "Low":
                series = close - 0.02
            elif field == "Close":
                series = close
            elif field == "Volume":
                series = pd.Series([1000.0] * len(index))
            else:
                series = pd.Series([100.0] * len(index))
            values[code] = series.tolist()
        fields[field] = pd.DataFrame(values, index=index)
    return fields


class FakeTQClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def get_market_data(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(dict(kwargs))
        return _response(list(kwargs["stock_list"]), str(kwargs["start_time"]))


class TQIntradaySnapshotTests(unittest.TestCase):
    def test_cli_requires_explicit_client_and_frozen_watchlist(self) -> None:
        args = build_parser().parse_args(
            [
                "tq-minute-snapshot",
                "--tdx-root",
                r"D:\\TdxQuant",
                "--watchlist",
                r"D:\\research\\watchlist.parquet",
            ]
        )
        self.assertEqual(args.command, "tq-minute-snapshot")
        self.assertEqual(args.period, "5m")
        self.assertTrue(args.output_dir.endswith("tq_intraday_research\\snapshots"))
        self.assertTrue(args.checkpoint_dir.endswith("tq_intraday_research\\checkpoints"))

    def test_plan_batches_never_crosses_sparse_dates_or_row_limit(self) -> None:
        batches = plan_tq_intraday_batches(
            [f"{index:06d}.SZ" for index in range(105)],
            ["2021-04-01", "2021-04-20"],
            max_codes_per_batch=100,
        )
        self.assertEqual(len(batches), 4)
        self.assertTrue(all(batch.start_date == batch.end_date for batch in batches))
        self.assertTrue(all(batch.estimated_records <= 24_000 for batch in batches))
        self.assertEqual({batch.start_date for batch in batches}, {"20210401", "20210420"})

    def test_file_capture_rejects_unfrozen_or_tampered_watchlist_before_tq(self) -> None:
        watchlist = pd.DataFrame(
            [
                {
                    "code": "000001.SZ",
                    "session_date": pd.Timestamp("2021-04-01"),
                    "signal_date": pd.Timestamp("2021-03-31"),
                }
            ]
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            loose_path = root / "watchlist.parquet"
            watchlist.to_parquet(loose_path, index=False)
            with self.assertRaisesRegex(TQDataError, "manifest not found"):
                capture_tq_watchlist_file(root / "tdx", loose_path, root / "snapshots")

            manifest = write_immutable_tq_watchlist(
                watchlist,
                root / "frozen",
                source_windows=[{"label": "dev"}],
            )
            frozen_path = root / "frozen" / manifest["watchlist_id"] / "watchlist.parquet"
            frozen_path.write_bytes(frozen_path.read_bytes() + b"tampered")
            with self.assertRaisesRegex(TQDataError, "hash mismatch"):
                capture_tq_watchlist_file(root / "tdx", frozen_path, root / "snapshots")

    def test_watchlist_plan_does_not_create_code_date_cross_product(self) -> None:
        watchlist = pd.DataFrame(
            [
                {"code": "000001.SZ", "session_date": "2021-04-01"},
                {"code": "600000.SH", "session_date": "2021-04-02"},
                {"code": "000001.SZ", "session_date": "2021-04-01"},
            ]
        )
        batches = plan_tq_watchlist_batches(watchlist)
        pairs = {(code, batch.start_date) for batch in batches for code in batch.codes}
        self.assertEqual(pairs, {("000001.SZ", "20210401"), ("600000.SH", "20210402")})

    def test_future_watchlist_rows_do_not_change_past_request_ids(self) -> None:
        past = pd.DataFrame([{"code": "000001.SZ", "session_date": "2021-04-01"}])
        extended = pd.concat(
            [past, pd.DataFrame([{"code": "600000.SH", "session_date": "2026-08-07"}])],
            ignore_index=True,
        )
        self.assertEqual(
            plan_tq_watchlist_batches(past)[0].batch_id,
            plan_tq_watchlist_batches(extended)[0].batch_id,
        )

    def test_acquisition_watchlist_excludes_future_labels(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "code": "000001.SZ",
                    "name": "Ping An Bank",
                    "signal_date": "2021-04-01",
                    "entry_date": "2021-04-02",
                    "hypothesis_id": "first_pullback_reclaim",
                    "selected": True,
                    "score": 0.8,
                    "raw_close": 10.0,
                    "net_return_1d": -0.9,
                    "exit_open_1d": 1.0,
                },
                {
                    "code": "000001.SZ",
                    "name": "Ping An Bank",
                    "signal_date": "2021-04-01",
                    "entry_date": "2021-04-02",
                    "hypothesis_id": "ma10_support_turn",
                    "selected": True,
                    "score": 0.7,
                    "raw_close": 10.0,
                    "net_return_1d": 0.9,
                    "exit_open_1d": 20.0,
                },
            ]
        )
        baseline = build_event_acquisition_watchlist({"dev": events})
        changed = events.copy()
        changed["net_return_1d"] = [100.0, -100.0]
        changed["exit_open_1d"] = [999.0, 0.01]
        pd.testing.assert_frame_equal(
            baseline,
            build_event_acquisition_watchlist({"dev": changed}),
        )
        self.assertEqual(len(baseline), 1)
        self.assertEqual(
            baseline.loc[0, "hypothesis_ids"],
            "first_pullback_reclaim,ma10_support_turn",
        )
        self.assertNotIn("net_return_1d", baseline.columns)

        with tempfile.TemporaryDirectory() as temp_dir:
            manifest = write_immutable_tq_watchlist(
                baseline,
                Path(temp_dir),
                source_windows=[{"label": "dev"}],
            )
            self.assertEqual(manifest["estimated_5m_rows"], 48)

    def test_normalize_and_validate_tq_response(self) -> None:
        bars = normalize_tq_market_data(
            _response(["000001.SZ", "600000.SH"], "20210401"),
            requested_codes=["000001.SZ", "600000.SH"],
        )
        self.assertEqual(len(bars), 96)
        self.assertEqual(float(bars["amount_scale"].iloc[0]), 10_000.0)
        self.assertEqual(float(bars["volume_scale"].iloc[0]), 100.0)
        normalized_ratio = float((bars["Amount"] / bars["Volume"] / bars["Close"]).median())
        self.assertTrue(0.5 <= normalized_ratio <= 1.5)
        quality = validate_tq_intraday_bars(
            bars,
            expected_code_sessions=[("000001.SZ", "2021-04-01"), ("600000.SH", "2021-04-01")],
            expected_bars_per_session=48,
            minimum_coverage=0.95,
        )
        self.assertTrue(quality["passed"])
        self.assertEqual(quality["coverage"], 1.0)

    def test_ambiguous_turnover_units_are_rejected(self) -> None:
        response = _response(["000001.SZ"], "20210401")
        response["Amount"] = response["Amount"] / 10.0
        with self.assertRaisesRegex(TQDataError, "infer TQ Amount/Volume units"):
            normalize_tq_market_data(response)

    def test_tq_error_and_missing_session_are_not_silently_accepted(self) -> None:
        with self.assertRaisesRegex(TQDataError, "ErrorId=7"):
            normalize_tq_market_data({"ErrorId": "7", "Msg": "disconnected"})
        bars = normalize_tq_market_data(_response(["000001.SZ"], "20210401"))
        with self.assertRaisesRegex(TQDataError, "missing_code_sessions"):
            validate_tq_intraday_bars(
                bars,
                expected_code_sessions=[("000001.SZ", "2021-04-01"), ("600000.SH", "2021-04-01")],
                expected_bars_per_session=48,
                minimum_coverage=0.50,
            )

    def test_fetch_uses_raw_unfilled_data_and_count_zero(self) -> None:
        client = FakeTQClient()
        batches = plan_tq_intraday_batches(["000001.SZ"], ["2021-04-01"])
        bars, report = fetch_tq_intraday_batches(client, batches)
        self.assertEqual(len(bars), 48)
        self.assertEqual(report["returned_rows"], 48)
        self.assertEqual(client.calls[0]["dividend_type"], "none")
        self.assertFalse(client.calls[0]["fill_data"])
        self.assertEqual(client.calls[0]["count"], 0)

    def test_batch_checkpoints_are_hash_verified_and_reused(self) -> None:
        batches = plan_tq_intraday_batches(["000001.SZ"], ["2021-04-01"])
        with tempfile.TemporaryDirectory() as temp_dir:
            first_client = FakeTQClient()
            first, first_report = fetch_tq_intraday_batches(
                first_client,
                batches,
                checkpoint_dir=Path(temp_dir),
            )
            second_client = FakeTQClient()
            second, second_report = fetch_tq_intraday_batches(
                second_client,
                batches,
                checkpoint_dir=Path(temp_dir),
            )
            pd.testing.assert_frame_equal(first, second)
            self.assertEqual(len(first_client.calls), 1)
            self.assertEqual(len(second_client.calls), 0)
            self.assertEqual(first_report["batches"][0]["source"], "tq")
            self.assertEqual(second_report["batches"][0]["source"], "checkpoint")

            parquet_path = next(Path(temp_dir).glob("*.parquet"))
            parquet_path.write_bytes(parquet_path.read_bytes() + b"corrupt")
            with self.assertRaisesRegex(TQDataError, "hash mismatch"):
                fetch_tq_intraday_batches(
                    FakeTQClient(),
                    batches,
                    checkpoint_dir=Path(temp_dir),
                )

    def test_snapshot_is_content_addressed_and_capture_is_replayable(self) -> None:
        bars = normalize_tq_market_data(_response(["000001.SZ"], "20210401"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = write_immutable_tq_snapshot(bars, root, source_query={"period": "5m"})
            second = write_immutable_tq_snapshot(bars, root, source_query={"period": "5m"})
            self.assertEqual(first["snapshot_id"], second["snapshot_id"])
            manifest_path = root / first["snapshot_id"] / "manifest.json"
            self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["bars_rows"], 48)

            watchlist = pd.DataFrame([{"code": "000001.SZ", "session_date": "2021-04-01"}])
            captured = capture_tq_intraday_snapshot(FakeTQClient(), watchlist, root)
            self.assertTrue(captured["quality_report"]["passed"])
            self.assertTrue((root / captured["snapshot_id"] / "bars.parquet").exists())


if __name__ == "__main__":
    unittest.main()
