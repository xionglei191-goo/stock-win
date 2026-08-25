from __future__ import annotations

import json
import unittest

import pandas as pd

from research_platform.leader_pullback_research import (
    assess_development_hypotheses,
    build_intraday_washout_events,
    build_ma20_bounce_events,
    build_pullback_event_table,
    build_reclaim_limit_order_events,
    build_reclaim_matched_pairs,
    build_trend_rsi2_events,
    build_two_day_exhaustion_events,
    simulate_event_portfolio,
    summarize_reclaim_matches,
    summarize_limit_order_events,
    summarize_intraday_washout_events,
    summarize_ma20_bounce_events,
    summarize_exhaustion_events,
    summarize_trend_rsi2_events,
    summarize_pullback_events,
)


CODE = "000001.SZ"
SIGNAL_OFFSET = 24


def pullback_bars(*, entry_open: float = 11.20, append_future: bool = False) -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=36, freq="B")
    close = [10.0] * 20 + [11.0, 11.5, 11.2, 10.8, 11.15]
    close += [11.30, 11.40, 11.55, 11.65, 11.75, 11.85]
    close += [11.90] * (len(index) - len(close))
    volume = [3_000_000.0] * 22 + [1_800_000.0] * (len(index) - 22)
    open_price = [value * 0.995 for value in close]
    open_price[20] = 10.10
    open_price[SIGNAL_OFFSET] = 10.90
    open_price[SIGNAL_OFFSET + 1] = entry_open
    frame = pd.DataFrame(
        {
            "code": CODE,
            "timestamp": index,
            "Open": open_price,
            "High": [value * 1.01 for value in close],
            "Low": [value * 0.99 for value in close],
            "Close": close,
            "Volume": volume,
            "Amount": [value * quantity for value, quantity in zip(close, volume)],
        }
    )
    if append_future:
        extra_index = pd.date_range(index[-1] + pd.Timedelta(days=1), periods=8, freq="B")
        extra = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": extra_index,
                "Open": 20.0,
                "High": 21.0,
                "Low": 19.0,
                "Close": 20.0,
                "Volume": 9_000_000.0,
                "Amount": 180_000_000.0,
            }
        )
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def market_states(
    *,
    score: float = 0.60,
    phase: str = "FERMENT",
    signal_offset: int = SIGNAL_OFFSET,
) -> pd.DataFrame:
    signal_date = pullback_bars()["timestamp"].iloc[signal_offset]
    state = {
        "market_phase": phase,
        "market_score": score,
        "market_regime": "NORMAL",
        "entry_allowed": True,
        "strong_sectors": [
            {
                "sector_code": "T001",
                "theme_phase": "FERMENT",
                "rank": 1,
                "score": 0.90,
            }
        ],
    }
    return pd.DataFrame(
        [
            {
                "timestamp": signal_date,
                "market_phase": phase,
                "market_style": "BROAD_RISK_ON",
                "entry_allowed": 1,
                "state_json": json.dumps(state),
            }
        ]
    )


def sector_membership() -> pd.DataFrame:
    return pd.DataFrame(
        [{"sector_code": "T001", "sector_name": "sample", "member_code": CODE}]
    )


def matched_bars(*, append_future: bool = False) -> pd.DataFrame:
    treated = pullback_bars(append_future=append_future)
    control = pullback_bars(append_future=append_future).copy()
    control["code"] = "000002.SZ"
    signal_index = control.index[
        control["timestamp"].eq(
            pullback_bars()["timestamp"].iloc[SIGNAL_OFFSET]
        )
    ][0]
    control.loc[signal_index, "Open"] = 11.10
    control.loc[signal_index, "Close"] = 11.05
    control.loc[signal_index, "High"] = 11.20
    control.loc[signal_index, "Low"] = 10.90
    control.loc[signal_index, "Amount"] = (
        control.loc[signal_index, "Close"] * control.loc[signal_index, "Volume"]
    )
    return pd.concat([treated, control], ignore_index=True)


def trend_oversold_bars() -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=85, freq="B")
    close = [10.0 + offset * 0.05 for offset in range(75)]
    close += [13.50, 13.00, 12.45, 12.80, 13.00, 13.15, 13.25, 13.30, 13.35, 13.40]
    volume = [10_000_000.0] * len(index)
    open_price = [value * 0.995 for value in close]
    open_price[77] = 12.70
    open_price[78] = 12.50
    return pd.DataFrame(
        {
            "code": CODE,
            "timestamp": index,
            "Open": open_price,
            "High": [max(open_value, close_value) * 1.01 for open_value, close_value in zip(open_price, close)],
            "Low": [min(open_value, close_value) * 0.99 for open_value, close_value in zip(open_price, close)],
            "Close": close,
            "Volume": volume,
            "Amount": [value * quantity for value, quantity in zip(close, volume)],
        }
    )


def ma20_bounce_bars(*, append_future: bool = False) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=86, freq="B")
    close = [10.0 + offset * 0.045 for offset in range(70)]
    close += [13.00, 12.90, 12.80, 12.70, 12.60, 12.70]
    close += [12.75, 12.82, 12.90, 13.00, 13.10, 13.18, 13.24, 13.30, 13.35, 13.40]
    volume = [10_000_000.0] * len(index)
    for offset in range(70, 76):
        volume[offset] = 6_000_000.0
    open_price = [value * 0.998 for value in close]
    open_price[75] = 12.62
    open_price[76] = 12.72
    frame = pd.DataFrame(
        {
            "code": CODE,
            "timestamp": index,
            "Open": open_price,
            "High": [
                max(open_value, close_value) * 1.005
                for open_value, close_value in zip(open_price, close)
            ],
            "Low": [
                min(open_value, close_value) * 0.995
                for open_value, close_value in zip(open_price, close)
            ],
            "Close": close,
            "Volume": volume,
            "Amount": [
                value * quantity for value, quantity in zip(close, volume)
            ],
        }
    )
    if append_future:
        extra_index = pd.date_range(
            index[-1] + pd.Timedelta(days=1), periods=8, freq="B"
        )
        extra = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": extra_index,
                "Open": 20.0,
                "High": 21.0,
                "Low": 19.0,
                "Close": 20.0,
                "Volume": 20_000_000.0,
                "Amount": 400_000_000.0,
            }
        )
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def intraday_washout_bars(*, append_future: bool = False) -> pd.DataFrame:
    index = pd.date_range("2024-01-02", periods=86, freq="B")
    close = [10.0 + offset * 0.045 for offset in range(75)]
    close += [12.95, 13.02, 13.12, 13.22, 13.30, 13.38, 13.45, 13.50, 13.55, 13.60, 13.65]
    volume = [10_000_000.0] * len(index)
    volume[75] = 18_000_000.0
    open_price = [value * 0.998 for value in close]
    high = [max(open_value, close_value) * 1.005 for open_value, close_value in zip(open_price, close)]
    low = [min(open_value, close_value) * 0.995 for open_value, close_value in zip(open_price, close)]
    open_price[75] = 12.65
    high[75] = 13.05
    low[75] = 12.55
    open_price[76] = 12.98
    frame = pd.DataFrame(
        {
            "code": CODE,
            "timestamp": index,
            "Open": open_price,
            "High": high,
            "Low": low,
            "Close": close,
            "Volume": volume,
            "Amount": [
                value * quantity for value, quantity in zip(close, volume)
            ],
        }
    )
    if append_future:
        extra_index = pd.date_range(
            index[-1] + pd.Timedelta(days=1), periods=8, freq="B"
        )
        extra = pd.DataFrame(
            {
                "code": CODE,
                "timestamp": extra_index,
                "Open": 20.0,
                "High": 21.0,
                "Low": 19.0,
                "Close": 20.0,
                "Volume": 20_000_000.0,
                "Amount": 400_000_000.0,
            }
        )
        frame = pd.concat([frame, extra], ignore_index=True)
    return frame


def market_state_on(signal_date: pd.Timestamp) -> pd.DataFrame:
    state = {
        "market_phase": "FERMENT",
        "market_score": 0.60,
        "market_regime": "NORMAL",
        "entry_allowed": True,
        "strong_sectors": [],
    }
    return pd.DataFrame(
        [
            {
                "timestamp": signal_date,
                "market_phase": "FERMENT",
                "market_style": "BROAD_RISK_ON",
                "entry_allowed": 1,
                "state_json": json.dumps(state),
            }
        ]
    )


class LeaderPullbackResearchTests(unittest.TestCase):
    def test_default_hypotheses_find_executable_reclaim(self) -> None:
        bars = pullback_bars()
        events = build_pullback_event_table(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_states(),
            sector_membership=sector_membership(),
        )

        signal_date = bars["timestamp"].iloc[SIGNAL_OFFSET]
        signal_events = events.loc[events["signal_date"].eq(signal_date)]
        self.assertIn("first_pullback_reclaim", set(signal_events["hypothesis_id"]))
        event = signal_events.loc[
            signal_events["hypothesis_id"].eq("first_pullback_reclaim")
        ].iloc[0]
        self.assertTrue(bool(event["selected"]))
        self.assertTrue(bool(event["executable"]))
        self.assertTrue(bool(event["market_gate"]))
        self.assertTrue(bool(event["theme_gate"]))
        self.assertEqual(event["matched_theme_code"], "T001")
        self.assertLess(event["pullback_volume_ratio"], 1.0)
        self.assertGreater(event["net_return_5d"], 0.0)

    def test_future_bars_do_not_change_historical_signal_features(self) -> None:
        baseline_bars = pullback_bars()
        future_bars = pullback_bars(append_future=True)
        baseline = build_pullback_event_table(
            baseline_bars,
            baseline_bars,
            {CODE: "sample"},
            market_states=market_states(),
            sector_membership=sector_membership(),
        )
        future = build_pullback_event_table(
            future_bars,
            future_bars,
            {CODE: "sample"},
            market_states=market_states(),
            sector_membership=sector_membership(),
        )
        cutoff = baseline_bars["timestamp"].iloc[SIGNAL_OFFSET]
        columns = [
            "code",
            "signal_date",
            "hypothesis_id",
            "score",
            "pullback_depth",
            "pullback_volume_ratio",
        ]
        left = baseline.loc[baseline["signal_date"].le(cutoff), columns].reset_index(drop=True)
        right = future.loc[future["signal_date"].le(cutoff), columns].reset_index(drop=True)
        pd.testing.assert_frame_equal(left, right)

    def test_next_open_gap_and_limit_up_are_distinct_blocks(self) -> None:
        gap_bars = pullback_bars(entry_open=12.10)
        gap_events = build_pullback_event_table(
            gap_bars,
            gap_bars,
            {CODE: "sample"},
            market_states=market_states(),
            sector_membership=sector_membership(),
        )
        gap_event = gap_events.loc[
            gap_events["hypothesis_id"].eq("first_pullback_reclaim")
        ].iloc[0]
        self.assertTrue(bool(gap_event["blocked_open_gap"]))
        self.assertFalse(bool(gap_event["blocked_limit_up_open"]))
        self.assertFalse(bool(gap_event["executable"]))

        limit_bars = pullback_bars(entry_open=12.30)
        limit_events = build_pullback_event_table(
            limit_bars,
            limit_bars,
            {CODE: "sample"},
            market_states=market_states(),
            sector_membership=sector_membership(),
        )
        limit_event = limit_events.loc[
            limit_events["hypothesis_id"].eq("first_pullback_reclaim")
        ].iloc[0]
        self.assertTrue(bool(limit_event["blocked_limit_up_open"]))
        self.assertFalse(bool(limit_event["blocked_open_gap"]))

    def test_healthy_divergence_requires_minimum_market_score(self) -> None:
        bars = pullback_bars()
        healthy = build_pullback_event_table(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_states(score=0.56, phase="DIVERGENCE"),
            sector_membership=sector_membership(),
        )
        weak = build_pullback_event_table(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_states(score=0.54, phase="DIVERGENCE"),
            sector_membership=sector_membership(),
        )
        self.assertTrue(bool(healthy["market_gate"].any()))
        self.assertFalse(bool(weak["market_gate"].any()))

    def test_summary_and_portfolio_apply_capacity_and_top_winner_check(self) -> None:
        events = pd.DataFrame(
            [
                {
                    "code": f"00000{index}.SZ",
                    "hypothesis_id": "sample",
                    "selected": True,
                    "executable": True,
                    "market_gate": True,
                    "theme_gate": index % 2 == 0,
                    "blocked_limit_up_open": False,
                    "blocked_open_gap": False,
                    "blocked_missing_bars": False,
                    "signal_date": pd.Timestamp("2025-01-01"),
                    "entry_date": pd.Timestamp("2025-01-02"),
                    "exit_date_5d": pd.Timestamp("2025-01-10"),
                    "net_return_5d": value,
                    "score": 1.0 - index * 0.01,
                }
                for index, value in enumerate([0.10, 0.05, 0.02, -0.01, -0.02], start=1)
            ]
        )
        portfolio = simulate_event_portfolio(events, trading_days=252)
        self.assertEqual(portfolio["portfolio_trades"], 3)
        self.assertGreater(portfolio["portfolio_total_return"], 0.0)

        summaries = summarize_pullback_events(events, trading_days=252)
        price_only = next(item for item in summaries if item["scope"] == "price_only")
        theme = next(item for item in summaries if item["scope"] == "market_and_theme")
        self.assertEqual(price_only["selected_signals"], 3)
        self.assertEqual(theme["selected_signals"], 2)

    def test_development_assessment_rejects_without_opening_holdout(self) -> None:
        reports = [
            {
                "window": {"label": label, "role": "DEVELOPMENT"},
                "summaries": [
                    {
                        "hypothesis_id": "sample",
                        "scope": "market",
                        "portfolio_trades": 40,
                        "portfolio_total_return": total_return,
                        "portfolio_annualized_return": total_return,
                        "portfolio_realized_max_drawdown": -0.05,
                        "portfolio_ex_top3_total_return": total_return - 0.01,
                        "portfolio_median_trade_return": 0.01,
                        "fill_rate": 0.90,
                    }
                ],
            }
            for label, total_return in (("dev1", 0.10), ("dev2", -0.02))
        ]

        decision = assess_development_hypotheses(reports)

        self.assertEqual(decision["decision"], "REJECT_ALL")
        self.assertIsNone(decision["selected_hypothesis"])
        self.assertFalse(decision["validation_opened"])
        self.assertFalse(decision["holdout_opened"])

    def test_reclaim_matching_uses_same_day_comparable_control(self) -> None:
        bars = matched_bars()
        pairs = build_reclaim_matched_pairs(
            bars,
            bars,
            {CODE: "treated", "000002.SZ": "control"},
            market_states=market_states(),
        )

        self.assertEqual(len(pairs), 1)
        pair = pairs.iloc[0]
        self.assertEqual(pair["treated_code"], CODE)
        self.assertEqual(pair["control_code"], "000002.SZ")
        self.assertEqual(pair["limit_ratio"], 0.10)
        self.assertLessEqual(pair["match_distance"], 4.0)
        self.assertAlmostEqual(
            pair["effect_net_return_5d"],
            pair["treated_net_return_5d"] - pair["control_net_return_5d"],
        )

        summary = summarize_reclaim_matches(pairs, bootstrap_samples=100)
        self.assertEqual(summary["pairs"], 1)
        self.assertEqual(summary["signal_days"], 1)
        self.assertEqual(len(summary["horizons"]), 3)

    def test_reclaim_matching_does_not_use_future_bars_in_pair_selection(self) -> None:
        baseline_bars = matched_bars()
        future_bars = matched_bars(append_future=True)
        baseline = build_reclaim_matched_pairs(
            baseline_bars,
            baseline_bars,
            {CODE: "treated", "000002.SZ": "control"},
            market_states=market_states(),
        )
        future = build_reclaim_matched_pairs(
            future_bars,
            future_bars,
            {CODE: "treated", "000002.SZ": "control"},
            market_states=market_states(),
        )
        columns = [
            "pair_id",
            "signal_date",
            "treated_code",
            "control_code",
            "match_distance",
            "effect_net_return_1d",
            "effect_net_return_3d",
            "effect_net_return_5d",
        ]
        pd.testing.assert_frame_equal(baseline[columns], future[columns])

    def test_three_percent_limit_entry_fills_only_when_next_day_trades_there(self) -> None:
        bars = pullback_bars()
        next_day = SIGNAL_OFFSET + 1
        bars.loc[next_day, "Low"] = 10.75
        events = build_reclaim_limit_order_events(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_states(),
        )
        signal_date = bars["timestamp"].iloc[SIGNAL_OFFSET]
        event = events.loc[events["signal_date"].eq(signal_date)].iloc[0]

        self.assertTrue(bool(event["filled_at_limit"]))
        self.assertFalse(bool(event["filled_at_open"]))
        self.assertTrue(bool(event["executable"]))
        self.assertAlmostEqual(event["limit_order_price"], 11.15 * 0.97)
        self.assertAlmostEqual(event["entry_price"], event["limit_order_price"])
        self.assertGreater(event["net_return_5d"], 0.0)

        summaries = summarize_limit_order_events(events, trading_days=252)
        market = next(item for item in summaries if item["scope"] == "market")
        self.assertEqual(market["executable_signals"], 1)
        self.assertEqual(market["unfilled_limit_order"], 0)

    def test_limit_entry_expires_and_respects_open_gap_block(self) -> None:
        untouched = pullback_bars()
        unfilled = build_reclaim_limit_order_events(
            untouched,
            untouched,
            {CODE: "sample"},
            market_states=market_states(),
        )
        signal_date = untouched["timestamp"].iloc[SIGNAL_OFFSET]
        unfilled_event = unfilled.loc[unfilled["signal_date"].eq(signal_date)].iloc[0]
        self.assertTrue(bool(unfilled_event["unfilled_limit_order"]))
        self.assertFalse(bool(unfilled_event["executable"]))

        gap = pullback_bars(entry_open=12.10)
        gap.loc[SIGNAL_OFFSET + 1, "Low"] = 10.75
        blocked = build_reclaim_limit_order_events(
            gap,
            gap,
            {CODE: "sample"},
            market_states=market_states(),
        )
        blocked_event = blocked.loc[blocked["signal_date"].eq(signal_date)].iloc[0]
        self.assertTrue(bool(blocked_event["blocked_open_gap"]))
        self.assertFalse(bool(blocked_event["executable"]))

    def test_two_day_exhaustion_uses_next_open_and_three_day_holding(self) -> None:
        bars = pullback_bars()
        exhaustion_offset = SIGNAL_OFFSET - 1
        bars.loc[exhaustion_offset, "Low"] = 10.50
        events = build_two_day_exhaustion_events(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_states(signal_offset=exhaustion_offset),
        )
        signal_date = bars["timestamp"].iloc[exhaustion_offset]
        event = events.loc[events["signal_date"].eq(signal_date)].iloc[0]

        self.assertTrue(bool(event["executable"]))
        self.assertEqual(event["entry_date"], bars["timestamp"].iloc[SIGNAL_OFFSET])
        self.assertGreater(event["net_return_3d"], 0.0)

        summaries = summarize_exhaustion_events(events, trading_days=252)
        market = next(item for item in summaries if item["scope"] == "market")
        self.assertEqual(market["holding_days"], 3)
        self.assertEqual(market["portfolio_trades"], 1)
        self.assertGreater(market["portfolio_total_return"], 0.0)

    def test_trend_rsi2_oversold_signal_uses_three_day_reversal(self) -> None:
        bars = trend_oversold_bars()
        signal_offset = 77
        signal_date = bars["timestamp"].iloc[signal_offset]
        events = build_trend_rsi2_events(
            bars,
            bars,
            {CODE: "sample"},
            market_states=market_state_on(signal_date),
        )
        event = events.loc[events["signal_date"].eq(signal_date)].iloc[0]

        self.assertLessEqual(event["rsi2"], 10.0)
        self.assertLessEqual(event["return_3d"], -0.05)
        self.assertGreater(event["return_60d"], 0.05)
        self.assertTrue(bool(event["executable"]))
        self.assertGreater(event["net_return_3d"], 0.0)

        summaries = summarize_trend_rsi2_events(events, trading_days=252)
        market = next(item for item in summaries if item["scope"] == "market")
        self.assertEqual(market["holding_days"], 3)
        self.assertEqual(market["portfolio_trades"], 1)

    def test_ma20_bounce_signal_is_point_in_time_and_holds_five_days(self) -> None:
        bars = ma20_bounce_bars()
        signal_offset = 75
        signal_date = bars["timestamp"].iloc[signal_offset]
        states = market_state_on(signal_date)
        events = build_ma20_bounce_events(
            bars,
            bars,
            {CODE: "sample"},
            market_states=states,
        )
        event = events.loc[events["signal_date"].eq(signal_date)].iloc[0]

        self.assertGreater(event["return_60d"], 0.10)
        self.assertGreater(event["ma20"], event["ma60"])
        self.assertGreater(event["ma20_slope_5d"], 0.0)
        self.assertLessEqual(abs(event["distance_to_ma20"]), 0.02)
        self.assertLessEqual(event["current_volume_ratio"], 0.85)
        self.assertTrue(bool(event["executable"]))
        self.assertGreater(event["net_return_5d"], 0.0)

        future_bars = ma20_bounce_bars(append_future=True)
        future = build_ma20_bounce_events(
            future_bars,
            future_bars,
            {CODE: "sample"},
            market_states=states,
        )
        columns = [
            "code",
            "signal_date",
            "score",
            "return_60d",
            "return_5d",
            "ma20_slope_5d",
            "distance_to_ma20",
            "current_volume_ratio",
        ]
        pd.testing.assert_frame_equal(
            events.loc[events["signal_date"].le(signal_date), columns].reset_index(drop=True),
            future.loc[future["signal_date"].le(signal_date), columns].reset_index(drop=True),
        )

        summaries = summarize_ma20_bounce_events(events, trading_days=252)
        market = next(item for item in summaries if item["scope"] == "market")
        self.assertEqual(market["portfolio_trades"], 1)
        self.assertGreater(market["portfolio_total_return"], 0.0)

    def test_intraday_washout_recovers_range_and_holds_three_days(self) -> None:
        bars = intraday_washout_bars()
        signal_offset = 75
        signal_date = bars["timestamp"].iloc[signal_offset]
        states = market_state_on(signal_date)
        events = build_intraday_washout_events(
            bars,
            bars,
            {CODE: "sample"},
            market_states=states,
        )
        event = events.loc[events["signal_date"].eq(signal_date)].iloc[0]

        self.assertLessEqual(event["intraday_low_return"], -0.05)
        self.assertGreaterEqual(event["close_location"], 0.70)
        self.assertGreaterEqual(event["current_volume_ratio"], 1.20)
        self.assertLess(event["current_return"], 0.0)
        self.assertTrue(bool(event["executable"]))
        self.assertGreater(event["net_return_3d"], 0.0)

        future_bars = intraday_washout_bars(append_future=True)
        future = build_intraday_washout_events(
            future_bars,
            future_bars,
            {CODE: "sample"},
            market_states=states,
        )
        columns = [
            "code",
            "signal_date",
            "score",
            "return_60d",
            "intraday_low_return",
            "close_location",
            "current_volume_ratio",
        ]
        pd.testing.assert_frame_equal(
            events.loc[events["signal_date"].le(signal_date), columns].reset_index(drop=True),
            future.loc[future["signal_date"].le(signal_date), columns].reset_index(drop=True),
        )

        summaries = summarize_intraday_washout_events(events, trading_days=252)
        market = next(item for item in summaries if item["scope"] == "market")
        self.assertEqual(market["holding_days"], 3)
        self.assertEqual(market["portfolio_trades"], 1)


if __name__ == "__main__":
    unittest.main()
