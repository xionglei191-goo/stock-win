from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from research_platform.config import USPortfolioConfig
from research_platform.strategies.us_momentum import USMomentumParameters
from research_platform.strategies.us_momentum_backtest import (
    StrictUSPointInTimeUniverse,
    run_backtest,
)
from research_platform.strategies.us_momentum_broad_backtest import (
    FAIL_CLOSED_MESSAGE,
    prefilter_universe,
)
from research_platform.us_pit import QualityReport, ReleaseStatus, USBacktestDataset


APPLE_ID = "us_apple_fixture"


def _bars(
    *,
    start: str = "2024-01-02",
    periods: int = 340,
    start_price: float = 50.0,
    end_price: float = 150.0,
) -> pd.DataFrame:
    index = pd.bdate_range(start, periods=periods)
    close = np.linspace(start_price, end_price, periods)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close * 1.01,
            "Low": close * 0.99,
            "Close": close,
            "Volume": np.full(periods, 2_000_000.0),
        },
        index=index,
    )


def _fixture(
    sessions: pd.DatetimeIndex,
    *,
    quality_ready: bool = True,
    aliases: pd.DataFrame | None = None,
    actions: pd.DataFrame | None = None,
    fee_schedule: pd.DataFrame | None = None,
) -> StrictUSPointInTimeUniverse:
    if aliases is None:
        aliases = pd.DataFrame(
            [
                {
                    "security_id": APPLE_ID,
                    "vendor_code": "AAPL.US",
                    "valid_from": sessions[0],
                    "valid_to": pd.NaT,
                }
            ]
        )
    return StrictUSPointInTimeUniverse(
        memberships={sessions[0]: {APPLE_ID}},
        source="test://strict-us-fixture",
        listing_aliases=aliases,
        trading_calendar=sessions,
        quality_report={
            "status": "DATA_READY" if quality_ready else "DATA_BLOCKED",
            "includes_delisted": quality_ready,
        },
        corporate_actions=actions if actions is not None else pd.DataFrame(),
        fee_schedule=(
            fee_schedule if fee_schedule is not None else pd.DataFrame()
        ),
    )


def _zero_cost() -> USPortfolioConfig:
    return USPortfolioConfig(
        commission_rate=0.0,
        slippage_rate=0.0,
        sec_sell_fee_rate=0.0,
        finra_taf_per_share=0.0,
    )


class StrictUSMomentumBacktestTests(unittest.TestCase):
    def test_data_ready_release_dataset_is_the_formal_entry(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        aliases = pd.DataFrame(
            [
                {
                    "security_id": APPLE_ID,
                    "vendor_code": "AAPL.US",
                    "valid_from": spy.index[0],
                    "valid_to": pd.NaT,
                }
            ]
        )
        fee_schedule = pd.DataFrame(
            [
                {
                    "effective_from": spy.index[0],
                    "effective_to": pd.NaT,
                    "commission_rate": 0.0,
                    "min_commission": 0.0,
                    "slippage_rate": 0.0,
                    "sec_sell_fee_rate": 0.0,
                    "finra_taf_per_share": 0.0,
                    "finra_taf_cap": 0.0,
                }
            ]
        )
        quality = QualityReport(
            policy_version="us-pit-quality-v1",
            status=ReleaseStatus.DATA_READY,
            includes_delisted=True,
            issues=(),
            metrics={"quality_contract_revision": 3},
        )
        dataset = USBacktestDataset(
            release_id="a" * 64,
            universe_id="sp500_ivv_proxy_v1",
            quality_report=quality,
            membership_by_date={spy.index[0]: frozenset({APPLE_ID})},
            security_master=pd.DataFrame(),
            identifiers=pd.DataFrame(),
            listing_aliases=aliases,
            corporate_actions=pd.DataFrame(),
            session_exceptions=pd.DataFrame(),
            calendar=pd.DataFrame({"session_date": spy.index}),
            fee_schedule=fee_schedule,
            raw_bars={APPLE_ID: apple},
            vendor_front_bars={APPLE_ID: apple},
            signal_bars_by_decision={spy.index[0]: {APPLE_ID: apple}},
            benchmark_bars={"SPY.US": spy},
        )

        result = run_backtest(
            dataset=dataset,
            names={APPLE_ID: "Apple"},
            params=USMomentumParameters(use_market_regime=False),
        )

        self.assertEqual(result["data_contract"]["release_id"], "a" * 64)
        self.assertTrue(any(row["side"] == "BUY" for row in result["trades"]))

    def test_production_path_requires_release_dataset(self) -> None:
        front = {"SPY.US": _bars(), APPLE_ID: _bars(end_price=180.0)}
        pit = _fixture(front["SPY.US"].index)

        with self.assertRaisesRegex(ValueError, "DATA_READY USBacktestDataset"):
            run_backtest(
                front,
                {},
                raw_bars=front,
                point_in_time_universe=pit,
            )

    def test_delisting_coverage_is_derived_not_a_caller_boolean(self) -> None:
        front = {"SPY.US": _bars(), APPLE_ID: _bars(end_price=180.0)}
        blocked = _fixture(front["SPY.US"].index, quality_ready=False)

        with self.assertRaisesRegex(ValueError, "quality_report"):
            run_backtest(
                front,
                {},
                raw_bars=front,
                point_in_time_universe=blocked,
                allow_test_fixture=True,
            )
        with self.assertRaises(TypeError):
            StrictUSPointInTimeUniverse(
                memberships={front["SPY.US"].index[0]: {APPLE_ID}},
                source="test://manual-boolean",
                includes_delisted=True,  # type: ignore[call-arg]
            )

    def test_strict_path_uses_stable_id_next_open_and_daily_raw_stop(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        raw_apple = apple.copy()
        raw_apple["Open"] = raw_apple["Close"]
        raw_apple["Low"] = raw_apple["Close"] * 0.99
        pit = _fixture(spy.index)

        result = run_backtest(
            {"SPY.US": spy, APPLE_ID: apple},
            {APPLE_ID: "Apple"},
            raw_bars={"SPY.US": spy, APPLE_ID: raw_apple},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )

        buys = [trade for trade in result["trades"] if trade["side"] == "BUY"]
        self.assertTrue(buys)
        first_buy = buys[0]
        buy_day = pd.Timestamp(first_buy["timestamp"])
        signal_day = spy.index[spy.index < buy_day][-1]
        self.assertNotEqual(buy_day.to_period("M"), signal_day.to_period("M"))
        self.assertAlmostEqual(
            first_buy["price"],
            float(raw_apple.loc[buy_day, "Open"]),
            places=6,
        )
        self.assertEqual(first_buy["security_id"], APPLE_ID)
        self.assertEqual(result["data_contract"]["stable_position_key"], "security_id")
        self.assertEqual(result["data_contract"]["calendar"], "frozen_xnys_release")

    def test_frozen_calendar_does_not_shrink_to_spy_intersection(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        missing_day = spy.index[100]
        shortened = spy.drop(index=missing_day)
        pit = _fixture(spy.index)

        with self.assertRaisesRegex(ValueError, "calendar coverage is incomplete"):
            run_backtest(
                {"SPY.US": shortened, APPLE_ID: apple},
                {},
                raw_bars={"SPY.US": shortened, APPLE_ID: apple},
                point_in_time_universe=pit,
                allow_test_fixture=True,
            )

    def test_midmonth_end_does_not_create_a_synthetic_month_end(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        pit = _fixture(spy.index)
        kwargs = dict(
            bars={"SPY.US": spy, APPLE_ID: apple},
            names={},
            raw_bars={"SPY.US": spy, APPLE_ID: apple},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
            start_date="2025-01-01",
        )

        through_february = run_backtest(end_date="2025-02-28", **kwargs)
        through_mid_march = run_backtest(end_date="2025-03-14", **kwargs)

        self.assertEqual(through_february["rebalances"], through_mid_march["rebalances"])
        self.assertTrue(through_mid_march["period"].startswith("2025-01-01"))
        self.assertTrue(through_mid_march["period"].endswith("2025-03-14"))
        self.assertTrue(
            all(row["timestamp"] <= "2025-03-14" for row in through_mid_march["trades"])
        )

    def test_split_is_applied_before_gap_stop_and_preserves_security_id(self) -> None:
        spy = _bars(periods=380)
        signal = _bars(periods=380, end_price=240.0)
        raw = signal.copy()
        split_day = spy.index[330]
        raw.loc[split_day:, ["Open", "High", "Low", "Close"]] /= 2.0
        action = pd.DataFrame(
            [
                {
                    "action_id": "split-1",
                    "security_id": APPLE_ID,
                    "action_type": "SPLIT",
                    "announced_at": split_day - pd.Timedelta(days=10),
                    "effective_at": split_day,
                    "terms_verified": True,
                    "split_ratio": 2.0,
                }
            ]
        )
        pit = _fixture(spy.index, actions=action)

        result = run_backtest(
            {"SPY.US": spy, APPLE_ID: signal},
            {},
            raw_bars={"SPY.US": spy, APPLE_ID: raw},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )

        reasons = [trade["reason"] for trade in result["trades"]]
        self.assertNotIn("US_FIXED_STOP_GAP", reasons)
        self.assertTrue(
            all(trade.get("security_id") == APPLE_ID for trade in result["trades"])
        )

    def test_split_can_migrate_a_position_to_a_successor_stable_id(self) -> None:
        successor_id = "us_apple_split_successor"
        spy = _bars(periods=380)
        signal = _bars(periods=380, end_price=240.0)
        split_day = spy.index[330]
        old_raw = signal.loc[signal.index < split_day].copy()
        new_raw = signal.loc[signal.index >= split_day].copy()
        new_raw[["Open", "High", "Low", "Close"]] /= 10.0
        aliases = pd.DataFrame(
            [
                {"security_id": APPLE_ID, "vendor_code": "OLD.US", "valid_from": spy.index[0], "valid_to": spy.index[329]},
                {"security_id": successor_id, "vendor_code": "NEW.US", "valid_from": split_day, "valid_to": pd.NaT},
            ]
        )
        actions = pd.DataFrame(
            [{
                "action_id": "split-successor",
                "security_id": APPLE_ID,
                "successor_security_id": successor_id,
                "action_type": "SPLIT",
                "announced_at": split_day - pd.Timedelta(days=10),
                "effective_at": split_day,
                "terms_verified": True,
                "split_ratio": 10.0,
            }]
        )
        pit = _fixture(spy.index, aliases=aliases, actions=actions)
        pit = StrictUSPointInTimeUniverse(
            memberships={spy.index[0]: {APPLE_ID}, split_day: {successor_id}},
            source=pit.source,
            listing_aliases=pit.listing_aliases,
            trading_calendar=pit.trading_calendar,
            quality_report=pit.quality_report,
            corporate_actions=pit.corporate_actions,
            fee_schedule=pit.fee_schedule,
        )

        result = run_backtest(
            {"SPY.US": spy, APPLE_ID: signal, successor_id: signal},
            {},
            raw_bars={"SPY.US": spy, APPLE_ID: old_raw, successor_id: new_raw},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )

        self.assertNotIn("US_FIXED_STOP_GAP", [row["reason"] for row in result["trades"]])
        self.assertTrue(
            any(
                row.get("security_id") == successor_id
                for row in result["open_positions"]
            )
        )

    def test_effective_fee_schedule_controls_execution_day(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        schedule = pd.DataFrame(
            [
                {
                    "effective_from": spy.index[0],
                    "effective_to": pd.Timestamp("2025-01-31"),
                    "commission_rate": 0.0,
                    "min_commission": 0.0,
                    "slippage_rate": 0.0,
                    "sec_sell_fee_rate": 0.0,
                    "finra_taf_per_share": 0.0,
                    "finra_taf_cap": 0.0,
                },
                {
                    "effective_from": pd.Timestamp("2025-02-01"),
                    "effective_to": pd.NaT,
                    "commission_rate": 0.01,
                    "min_commission": 0.0,
                    "slippage_rate": 0.0,
                    "sec_sell_fee_rate": 0.0,
                    "finra_taf_per_share": 0.0,
                    "finra_taf_cap": 0.0,
                }
            ]
        )
        pit = _fixture(spy.index, fee_schedule=schedule)

        result = run_backtest(
            {"SPY.US": spy, APPLE_ID: apple},
            {},
            raw_bars={"SPY.US": spy, APPLE_ID: apple},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )

        buy = next(trade for trade in result["trades"] if trade["side"] == "BUY")
        self.assertAlmostEqual(
            buy["fees"],
            buy["quantity"] * buy["price"] * 0.01,
            places=5,
        )

    def test_ticker_change_keeps_the_stable_position(self) -> None:
        spy = _bars(periods=380)
        apple = _bars(periods=380, end_price=240.0)
        rename_day = spy.index[320]
        aliases = pd.DataFrame(
            [
                {
                    "security_id": APPLE_ID,
                    "vendor_code": "AAPL.US",
                    "valid_from": spy.index[0],
                    "valid_to": spy.index[319],
                },
                {
                    "security_id": APPLE_ID,
                    "vendor_code": "APPLX.US",
                    "valid_from": rename_day,
                    "valid_to": pd.NaT,
                },
            ]
        )
        actions = pd.DataFrame(
            [
                {
                    "action_id": "rename-1",
                    "security_id": APPLE_ID,
                    "action_type": "TICKER_CHANGE",
                    "announced_at": rename_day - pd.Timedelta(days=7),
                    "effective_at": rename_day,
                    "terms_verified": True,
                }
            ]
        )
        pit = _fixture(spy.index, aliases=aliases, actions=actions)

        result = run_backtest(
            {"SPY.US": spy, APPLE_ID: apple},
            {},
            raw_bars={"SPY.US": spy, APPLE_ID: apple},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )

        self.assertTrue(result["open_positions"])
        self.assertEqual(result["open_positions"][0]["security_id"], APPLE_ID)
        self.assertEqual(result["open_positions"][0]["code"], "APPLX.US")
        self.assertFalse(
            any(row["reason"] == "US_PIT_MEMBERSHIP_EXIT" for row in result["trades"])
        )

    def test_unverified_held_corporate_action_fails_closed(self) -> None:
        spy = _bars(periods=380)
        apple = _bars(periods=380, end_price=240.0)
        action_day = spy.index[320]
        actions = pd.DataFrame(
            [
                {
                    "action_id": "unknown-1",
                    "security_id": APPLE_ID,
                    "action_type": "CASH_MERGER",
                    "announced_at": action_day - pd.Timedelta(days=7),
                    "effective_at": action_day,
                    "terms_verified": False,
                }
            ]
        )
        pit = _fixture(spy.index, actions=actions)

        with self.assertRaisesRegex(ValueError, "Unverified corporate action"):
            run_backtest(
                {"SPY.US": spy, APPLE_ID: apple},
                {},
                raw_bars={"SPY.US": spy, APPLE_ID: apple},
                point_in_time_universe=pit,
                params=USMomentumParameters(use_market_regime=False),
                cost_config=_zero_cost(),
                allow_test_fixture=True,
            )

    def test_future_signal_rows_do_not_change_first_decision(self) -> None:
        spy = _bars()
        apple = _bars(end_price=220.0)
        pit = _fixture(spy.index)
        kwargs = dict(
            names={},
            raw_bars={"SPY.US": spy, APPLE_ID: apple},
            point_in_time_universe=pit,
            params=USMomentumParameters(use_market_regime=False),
            cost_config=_zero_cost(),
            allow_test_fixture=True,
        )
        original = run_backtest({"SPY.US": spy, APPLE_ID: apple}, **kwargs)
        changed = apple.copy()
        changed.iloc[-20:, changed.columns.get_loc("Close")] *= 100.0
        replay = run_backtest({"SPY.US": spy, APPLE_ID: changed}, **kwargs)

        first_original = next(x for x in original["trades"] if x["side"] == "BUY")
        first_replay = next(x for x in replay["trades"] if x["side"] == "BUY")
        self.assertEqual(first_original, first_replay)

    def test_legacy_broad_universe_entry_is_disabled(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "survivorship"):
            prefilter_universe()
        self.assertIn("point-in-time", FAIL_CLOSED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
