from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from research_platform.lhb_seat_detail_store import (
    LhbSeatDetailStore,
    LhbSeatDetailStoreError,
)


def _row(**overrides):
    base = {
        "exchange": "SSE",
        "code": "600000",
        "trade_date": "2026-08-20",
        "publish_date": "2026-08-21",
        "seat_side": "buy",
        "seat_rank": 1,
        "seat_name_raw": "机构专用",
        "buy_amount": 12345.6,
        "sell_amount": None,
        "source": "test",
    }
    base.update(overrides)
    return base


class LhbSeatDetailStoreTests(unittest.TestCase):
    def test_inserts_deduplicates_and_never_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LhbSeatDetailStore(Path(tmp) / "lhb.db")
            first = store.record_rows([_row(), _row(seat_side="sell", seat_rank=1)])
            self.assertEqual(first["inserted"], 2)
            duplicate = store.record_rows([_row()])
            self.assertEqual(duplicate["submitted"], 1)
            self.assertEqual(duplicate["unique"], 1)
            self.assertEqual(duplicate["inserted"], 0)

            coverage = store.coverage()
            self.assertEqual(coverage["row_count"], 2)
            self.assertEqual(coverage["distinct_codes"], 1)
            self.assertEqual(coverage["distinct_publish_dates"], 1)
            self.assertEqual(coverage["earliest_trade_date"], "2026-08-20")
            self.assertEqual(coverage["rows_by_source"], {"test": 2})

    def test_rejects_invalid_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LhbSeatDetailStore(Path(tmp) / "lhb.db")
            with self.assertRaises(LhbSeatDetailStoreError):
                store.record_rows([{"code": "600000"}])
            with self.assertRaises(LhbSeatDetailStoreError):
                store.record_rows([_row(seat_side="both")])
            with self.assertRaises(LhbSeatDetailStoreError):
                store.record_rows([_row(buy_amount="big")])
            with self.assertRaises(LhbSeatDetailStoreError):
                store.record_rows("not-a-list")  # type: ignore[arg-type]

    def test_batch_internal_duplicates_collapse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LhbSeatDetailStore(Path(tmp) / "lhb.db")
            result = store.record_rows([_row(), _row()])
            self.assertEqual(result["submitted"], 2)
            self.assertEqual(result["unique"], 1)
            self.assertEqual(result["inserted"], 1)

    def test_publish_date_optional_in_payload_but_keyed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = LhbSeatDetailStore(Path(tmp) / "lhb.db")
            store.record_rows([_row(publish_date=None)])
            coverage = store.coverage()
            self.assertEqual(coverage["row_count"], 1)
            self.assertEqual(coverage["distinct_publish_dates"], 0)


if __name__ == "__main__":
    unittest.main()
