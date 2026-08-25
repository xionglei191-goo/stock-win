from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.panic_reversal_research import (
    HYPOTHESIS_ID,
    assess_development,
    build_panic_reversal_events,
    protocol_manifest,
)


def _bars(*, recent_limit: bool = False) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    dates = pd.bdate_range("2023-01-02", periods=135)
    close = np.linspace(10.0, 15.5, len(dates))
    signal_index = 130
    close[signal_index - 3] = 15.0
    close[signal_index - 2] = 14.3
    close[signal_index - 1] = 13.5
    close[signal_index] = 12.9
    if recent_limit:
        close[signal_index - 10] = close[signal_index - 11] * 1.10
    open_values = close * 0.995
    high = np.maximum(open_values, close) * 1.01
    low = np.minimum(open_values, close) * 0.99
    prior_close = close[signal_index - 1]
    open_values[signal_index] = 12.1
    low[signal_index] = prior_close * 0.915
    high[signal_index] = 13.0
    close[signal_index] = 12.9
    volume = np.full(len(dates), 10_000_000.0)
    volume[signal_index] = 20_000_000.0
    amount = volume * close
    front = pd.DataFrame(
        {
            "code": "600001.SH",
            "timestamp": dates,
            "Open": open_values,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
            "Amount": amount,
        }
    )
    raw = front.copy()
    raw.loc[signal_index + 1, "Open"] = close[signal_index] * 1.01
    raw.loc[signal_index + 4, "Open"] = close[signal_index] * 1.08
    states = pd.DataFrame(
        {
            "timestamp": dates,
            "market_phase": "RECOVERY",
            "market_style": "BROAD_RISK_ON",
            "entry_allowed": True,
            "state_json": "{}",
        }
    )
    return front, raw, states


class PanicReversalResearchTests(unittest.TestCase):
    def test_signal_requires_multi_day_panic_and_absorption(self) -> None:
        front, raw, states = _bars()
        events = build_panic_reversal_events(
            front,
            raw,
            {"600001.SH": "SAMPLE"},
            market_states=states,
        )
        self.assertEqual(len(events), 1)
        event = events.iloc[0]
        self.assertEqual(event["hypothesis_id"], HYPOTHESIS_ID)
        self.assertTrue(event["market_gate"])
        self.assertTrue(event["executable"])
        self.assertGreater(event["rebound_from_low"], 0.04)
        self.assertLess(event["return_3d"], -0.12)

    def test_recent_limit_up_excludes_v9_like_stock(self) -> None:
        front, raw, states = _bars(recent_limit=True)
        events = build_panic_reversal_events(
            front,
            raw,
            {"600001.SH": "SAMPLE"},
            market_states=states,
        )
        self.assertTrue(events.empty)

    def test_appended_future_does_not_change_signal(self) -> None:
        front, raw, states = _bars()
        first = build_panic_reversal_events(
            front,
            raw,
            {"600001.SH": "SAMPLE"},
            market_states=states,
        )
        next_date = front["timestamp"].max() + pd.offsets.BDay(1)
        future_front = pd.concat(
            [
                front,
                pd.DataFrame(
                    {
                        "code": ["600001.SH"],
                        "timestamp": [next_date],
                        "Open": [99.0],
                        "High": [100.0],
                        "Low": [98.0],
                        "Close": [99.0],
                        "Volume": [1.0],
                        "Amount": [99.0],
                    }
                ),
            ],
            ignore_index=True,
        )
        future_raw = pd.concat([raw, future_front.tail(1)], ignore_index=True)
        future_states = pd.concat(
            [
                states,
                pd.DataFrame(
                    {
                        "timestamp": [next_date],
                        "market_phase": ["ICE"],
                        "market_style": ["DEFENSIVE"],
                        "entry_allowed": [False],
                        "state_json": ["{}"],
                    }
                ),
            ],
            ignore_index=True,
        )
        second = build_panic_reversal_events(
            future_front,
            future_raw,
            {"600001.SH": "SAMPLE"},
            market_states=future_states,
        )
        first_keys = first.loc[:, ["signal_date", "code"]].reset_index(drop=True)
        second_keys = second.loc[
            pd.to_datetime(second["signal_date"]).le(front["timestamp"].max()),
            ["signal_date", "code"],
        ].reset_index(drop=True)
        pd.testing.assert_frame_equal(first_keys, second_keys)

    def test_development_gate_requires_both_windows_and_double_cost(self) -> None:
        def report(label: str, *, total: float = 0.03) -> dict[str, object]:
            return {
                "window": {"label": label},
                "summary": {
                    "portfolio_trades": 25,
                    "portfolio_annualized_return": 0.03,
                    "portfolio_total_return": total,
                    "portfolio_median_trade_return": 0.01,
                    "portfolio_ex_top3_total_return": 0.01,
                    "portfolio_realized_max_drawdown": -0.05,
                    "fill_rate": 0.80,
                },
            }

        base = [report("a"), report("b")]
        stress = [report("a"), report("b")]
        passed = assess_development(base, stress)
        self.assertEqual(passed["decision"], "OPEN_REPLICATION")
        self.assertFalse(passed["production_authorized"])
        rejected = assess_development(base, [report("a", total=-0.01), report("b")])
        self.assertEqual(rejected["decision"], "REJECT")

    def test_protocol_keeps_later_windows_sealed(self) -> None:
        protocol = protocol_manifest()
        self.assertEqual(protocol["research_status"], "development_only")
        self.assertEqual(
            protocol["sequence"]["replication_window_sealed"]["role"], "REPLICATION"
        )
        self.assertTrue(protocol["invariants"]["no_production_registration"])
        self.assertTrue(protocol["invariants"]["passing_is_not_automatic_promotion"])


if __name__ == "__main__":
    unittest.main()
