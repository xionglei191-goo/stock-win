from __future__ import annotations

import math
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from research_platform.config import USPortfolioConfig
from research_platform.strategies.us_momentum import USMomentumParameters
from research_platform.strategies.us_momentum_backtest import _fees_on
from research_platform.us_pit.dataset import USBacktestDataset
from research_platform.us_pit.models import (
    QUALITY_POLICY_VERSION,
    QualityReport,
    ReleaseStatus,
    UNIVERSE_ID,
)
from research_platform.us_qualification import (
    HistoricalQualificationService,
    HistoricalRunRequest,
    PaperCycleEvidence,
    PaperQualificationTracker,
    PaperSessionEvidence,
    PaperTradeEvidence,
    QualificationError,
    SealedHoldoutError,
    TDXDailySymbolEvidence,
    TDX_QUALIFICATION_SAMPLE,
    evaluate_tdx_quote_qualification,
    run_strict_qualification_backtest,
)


NY = ZoneInfo("America/New_York")
HASH_A = "a" * 64
HASH_B = "b" * 64
HASH_C = "c" * 64


def _trend(index: pd.DatetimeIndex, annual_return: float) -> pd.Series:
    periods = np.arange(len(index), dtype=float)
    return pd.Series(100.0 * (1.0 + annual_return) ** (periods / 252.0), index=index)


def _spy_with_bear_months(index: pd.DatetimeIndex) -> pd.Series:
    values: list[float] = []
    price = 100.0
    current: tuple[int, int] | None = None
    month_number = -1
    for stamp in index:
        month = (stamp.year, stamp.month)
        if month != current:
            month_number += 1
            current = month
            price *= 0.97 if month_number % 6 == 5 else 1.02
        values.append(price)
    return pd.Series(values, index=index)


def _dataset() -> USBacktestDataset:
    decisions = pd.date_range("2020-01-31", periods=60, freq="ME")
    sessions = pd.bdate_range("2019-01-02", "2025-01-10")
    spy = _spy_with_bear_months(sessions)
    bil = _trend(sessions, 0.04)
    master = pd.DataFrame(
        {
            "security_id": [f"us_security_{index}" for index in range(14)],
            "issuer_id": [f"issuer-{index}" for index in range(14)],
        }
    )
    report = QualityReport(
        policy_version=QUALITY_POLICY_VERSION,
        status=ReleaseStatus.DATA_READY,
        includes_delisted=True,
        issues=(),
        metrics={"quality_contract_revision": 3},
    )
    return USBacktestDataset(
        release_id=HASH_A,
        universe_id=UNIVERSE_ID,
        quality_report=report,
        membership_by_date={
            stamp: frozenset(master["security_id"]) for stamp in decisions
        },
        security_master=master,
        identifiers=pd.DataFrame(),
        listing_aliases=pd.DataFrame(),
        corporate_actions=pd.DataFrame(),
        session_exceptions=pd.DataFrame(),
        calendar=pd.DataFrame({"session_date": sessions}),
        fee_schedule=pd.DataFrame(
            {
                "effective_from": [pd.Timestamp("2019-01-01")],
                "commission_rate": [0.0005],
                "slippage_rate": [0.0005],
            }
        ),
        raw_bars={},
        vendor_front_bars={},
        signal_bars_by_decision={},
        benchmark_bars={
            "SPY.US": pd.DataFrame(
                {"close": spy, "total_return_close": spy}
            ),
            "BIL.US": pd.DataFrame(
                {"close": bil, "total_return_close": bil}
            ),
        },
    )


class _PassingRunner:
    def __init__(self, dataset: USBacktestDataset) -> None:
        self.dataset = dataset
        self.requests = []

    def __call__(self, dataset, request):
        self.requests.append(request)
        index = dataset.benchmark_bars["BIL.US"].loc[
            pd.Timestamp(request.start_date) : pd.Timestamp(request.end_date)
        ].index
        targets = {
            "development": 0.12,
            "validation": 0.12,
            "sealed": 0.10,
            "oos_24m": 0.12,
            "top_issuer_removed": 0.07,
            "double_cost": 0.08,
            "entry_20": 0.10,
            "entry_30": 0.10,
            "exit_35": 0.10,
            "exit_45": 0.10,
            "stop_06": 0.10,
            "stop_10": 0.10,
        }
        target = targets[request.run_id]
        curve = _trend(index, target)
        trades = []
        if request.run_id == "oos_24m":
            for number in range(35):
                trades.append(
                    {
                        "side": "SELL",
                        "security_id": f"us_security_{number % 14}",
                        "pnl": 100.0 if number % 14 == 0 else 10.0,
                    }
                )
        return {
            "period": f"{request.start_date}/{request.end_date}",
            "equity_curve": {
                stamp.date().isoformat(): float(value)
                for stamp, value in curve.items()
            },
            "trades": trades,
            "data_contract": {"release_id": dataset.release_id},
        }


class HistoricalQualificationTests(unittest.TestCase):
    def test_raw_close_benchmark_cannot_unlock_promotion(self) -> None:
        dataset = _dataset()
        dataset.benchmark_bars["BIL.US"].drop(
            columns=["total_return_close"], inplace=True
        )
        service = HistoricalQualificationService()

        with self.assertRaisesRegex(QualificationError, "total-return"):
            service.qualify(
                dataset,
                _PassingRunner(dataset),
                parameters={
                    "rs_top_pct": 0.25,
                    "exit_top_pct": 0.40,
                    "stop_ratio": 0.08,
                },
                strategy_code_sha256=HASH_B,
            )

    def test_locked_protocol_passes_and_runs_exact_neighborhoods(self) -> None:
        dataset = _dataset()
        runner = _PassingRunner(dataset)
        service = HistoricalQualificationService()

        result = service.qualify(
            dataset,
            runner,
            parameters={
                "rs_top_pct": 0.25,
                "exit_top_pct": 0.40,
                "stop_ratio": 0.08,
            },
            strategy_code_sha256=HASH_B,
        )

        self.assertTrue(result.decision.qualified, result.decision.failures)
        self.assertEqual(result.decision.status, "BACKTEST_QUALIFIED")
        self.assertEqual(result.split.development_months, 36)
        self.assertEqual(result.split.validation_months, 12)
        self.assertEqual(result.split.sealed_months, 12)
        request_by_id = {request.run_id: request for request in runner.requests}
        self.assertEqual(request_by_id["double_cost"].commission_multiplier, 2.0)
        self.assertEqual(request_by_id["double_cost"].slippage_multiplier, 2.0)
        self.assertTrue(request_by_id["top_issuer_removed"].excluded_security_ids)
        self.assertEqual(
            {
                "entry_20",
                "entry_30",
                "exit_35",
                "exit_45",
                "stop_06",
                "stop_10",
            },
            set(result.run_sha256)
            & {
                "entry_20",
                "entry_30",
                "exit_35",
                "exit_45",
                "stop_06",
                "stop_10",
            },
        )

    def test_same_frozen_holdout_cannot_be_opened_twice(self) -> None:
        dataset = _dataset()
        with tempfile.TemporaryDirectory() as temporary:
            registry = Path(temporary) / "sealed.json"
            first = HistoricalQualificationService(registry)
            first.qualify(
                dataset,
                _PassingRunner(dataset),
                parameters={
                    "rs_top_pct": 0.25,
                    "exit_top_pct": 0.40,
                    "stop_ratio": 0.08,
                },
                strategy_code_sha256=HASH_B,
            )
            second = HistoricalQualificationService(registry)
            with self.assertRaisesRegex(SealedHoldoutError, "already been opened"):
                second.qualify(
                    dataset,
                    _PassingRunner(dataset),
                    parameters={
                        "rs_top_pct": 0.25,
                        "exit_top_pct": 0.40,
                        "stop_ratio": 0.08,
                    },
                    strategy_code_sha256=HASH_B,
                )

    def test_non_ready_dataset_fails_before_runner(self) -> None:
        dataset = _dataset()
        blocked_report = replace(
            dataset.quality_report,
            status=ReleaseStatus.DATA_BLOCKED,
        )
        dataset = replace(dataset, quality_report=blocked_report)
        runner = _PassingRunner(dataset)

        with self.assertRaisesRegex(QualificationError, "DATA_READY"):
            HistoricalQualificationService().qualify(
                dataset,
                runner,
                parameters={
                    "rs_top_pct": 0.25,
                    "exit_top_pct": 0.40,
                    "stop_ratio": 0.08,
                },
                strategy_code_sha256=HASH_B,
            )
        self.assertFalse(runner.requests)

    def test_calendar_schema_must_be_explicit_not_a_range_index(self) -> None:
        dataset = replace(_dataset(), calendar=pd.DataFrame(index=range(10)))

        with self.assertRaisesRegex(QualificationError, "lacks session_date"):
            HistoricalQualificationService().qualify(
                dataset,
                _PassingRunner(dataset),
                parameters={
                    "rs_top_pct": 0.25,
                    "exit_top_pct": 0.40,
                    "stop_ratio": 0.08,
                },
                strategy_code_sha256=HASH_B,
            )


class StrictQualificationAdapterTests(unittest.TestCase):
    @patch("research_platform.strategies.us_momentum_backtest.run_backtest")
    def test_adapter_maps_parameters_costs_and_ephemeral_exclusion(
        self, mocked_run
    ) -> None:
        dataset = _dataset()
        original_members = {
            decision: frozenset(members)
            for decision, members in dataset.membership_by_date.items()
        }
        mocked_run.return_value = {
            "equity_curve": {"2024-01-02": 100_000.0, "2024-01-03": 100_100.0},
            "trades": [],
            "data_contract": {"release_id": dataset.release_id},
        }
        request = HistoricalRunRequest(
            run_id="double_cost",
            start_date=date(2023, 1, 1),
            end_date=date(2024, 12, 31),
            parameters={
                "rs_top_pct": 0.25,
                "exit_top_pct": 0.40,
                "stop_ratio": 0.08,
                "use_market_regime": False,
            },
            commission_multiplier=2.0,
            slippage_multiplier=3.0,
            excluded_security_ids=frozenset({"us_security_0"}),
        )

        result = run_strict_qualification_backtest(dataset, request)

        kwargs = mocked_run.call_args.kwargs
        self.assertIsInstance(kwargs["params"], USMomentumParameters)
        self.assertEqual(kwargs["params"].rs_top_pct, 0.25)
        self.assertEqual(kwargs["commission_multiplier"], 2.0)
        self.assertEqual(kwargs["slippage_multiplier"], 3.0)
        filtered = kwargs["dataset"]
        self.assertTrue(
            all(
                "us_security_0" not in members
                for members in filtered.membership_by_date.values()
            )
        )
        self.assertEqual(dataset.membership_by_date, original_members)
        self.assertEqual(
            result["data_contract"]["excluded_security_ids"], ["us_security_0"]
        )

    @patch("research_platform.strategies.us_momentum_backtest.run_backtest")
    def test_adapter_rejects_unknown_exclusion_before_engine(self, mocked_run) -> None:
        request = HistoricalRunRequest(
            run_id="top_issuer_removed",
            start_date=date(2023, 1, 1),
            end_date=date(2024, 12, 31),
            parameters={
                "rs_top_pct": 0.25,
                "exit_top_pct": 0.40,
                "stop_ratio": 0.08,
            },
            excluded_security_ids=frozenset({"us_unknown_security"}),
        )

        with self.assertRaisesRegex(QualificationError, "absent from the frozen release"):
            run_strict_qualification_backtest(_dataset(), request)
        mocked_run.assert_not_called()

    def test_effective_fee_row_is_scaled_with_independent_multipliers(self) -> None:
        schedule = pd.DataFrame(
            {
                "effective_from": [pd.Timestamp("2024-01-01")],
                "effective_to": [pd.NaT],
                "commission_rate": [0.001],
                "min_commission": [2.0],
                "slippage_rate": [0.002],
                "sec_sell_fee_rate": [0.00002],
                "finra_taf_per_share": [0.0001],
                "finra_taf_cap": [8.0],
            }
        )

        terms = _fees_on(
            schedule,
            pd.Timestamp("2024-06-03"),
            USPortfolioConfig(),
            commission_multiplier=2.0,
            slippage_multiplier=3.0,
        )

        self.assertAlmostEqual(terms.commission_rate, 0.002)
        self.assertAlmostEqual(terms.min_commission, 4.0)
        self.assertAlmostEqual(terms.slippage_rate, 0.006)
        self.assertAlmostEqual(terms.sec_sell_fee_rate, 0.00002)
        self.assertAlmostEqual(terms.finra_taf_per_share, 0.0001)
        self.assertAlmostEqual(terms.finra_taf_cap, 8.0)


def _paper_evidence(count: int = 252):
    sessions = [stamp.date() for stamp in pd.bdate_range("2024-01-02", periods=260)]
    selected = sessions[:count]
    equity = np.linspace(80.0, 110.0, count)
    if count:
        equity[0] = 100.0
    if count > 1:
        equity[1] = 80.0
    session_rows = [
        PaperSessionEvidence(
            session=session,
            equity=float(equity[index]),
            bil_equity=float(100.0 + 4.0 * index / max(1, count - 1)),
            input_sha256=HASH_A,
            output_sha256=HASH_B,
            replay_output_sha256=HASH_B,
        )
        for index, session in enumerate(selected)
    ]
    cycles = []
    used_months = set()
    for index, session in enumerate(selected[:-1]):
        month = (session.year, session.month)
        if month in used_months:
            continue
        used_months.add(month)
        cycles.append(
            PaperCycleEvidence(
                cycle_id=f"cycle-{index}",
                decision_session=session,
                execution_session=selected[index + 1],
                complete=True,
                replay_verified=True,
            )
        )
        if len(cycles) == 12:
            break
    trades = [
        PaperTradeEvidence(
            trade_id=f"trade-{index}",
            opened_session=selected[index],
            closed_session=selected[index + 1],
        )
        for index in range(min(20, max(0, len(selected) - 1)))
    ]
    return sessions, session_rows, cycles, trades


class PaperQualificationTests(unittest.TestCase):
    def test_all_boundaries_pass_including_exact_twenty_percent_drawdown(self) -> None:
        sessions, session_rows, cycles, trades = _paper_evidence()
        decision = PaperQualificationTracker(sessions).evaluate(
            session_rows, cycles, trades
        )

        self.assertTrue(decision.qualified, decision.failures)
        self.assertEqual(decision.status, "PAPER_QUALIFIED")
        self.assertAlmostEqual(float(decision.metrics["max_drawdown"]), 0.20)

    def test_incomplete_duration_stays_collecting(self) -> None:
        sessions, session_rows, cycles, trades = _paper_evidence(251)
        decision = PaperQualificationTracker(sessions).evaluate(
            session_rows, cycles, trades
        )

        self.assertFalse(decision.qualified)
        self.assertEqual(decision.status, "PAPER_COLLECTING")
        self.assertIn("sessions", decision.failures)

    def test_replay_mismatch_blocks_paper(self) -> None:
        sessions, session_rows, cycles, trades = _paper_evidence()
        session_rows[-1] = replace(session_rows[-1], replay_output_sha256=HASH_C)
        decision = PaperQualificationTracker(sessions).evaluate(
            session_rows, cycles, trades
        )

        self.assertFalse(decision.qualified)
        self.assertEqual(decision.status, "PAPER_BLOCKED")
        self.assertIn("replayable", decision.failures)


def _tdx_evidence(session_count: int = 20):
    sessions = [stamp.date() for stamp in pd.bdate_range("2025-01-02", periods=25)]
    rows = []
    for session in sessions[:session_count]:
        for instrument in TDX_QUALIFICATION_SAMPLE:
            observed = datetime.combine(session, datetime.min.time(), NY).replace(
                hour=9, minute=31
            )
            rows.append(
                TDXDailySymbolEvidence(
                    session=session,
                    symbol=instrument.symbol,
                    exchange=instrument.exchange,
                    expected_poll_slots=390,
                    captured_poll_slots=390,
                    fresh_poll_slots=390,
                    poll_interval_seconds=60,
                    maximum_source_latency_seconds=30.0,
                    opening_observed_at=observed,
                    opening_source_at=observed - timedelta(seconds=30),
                    snapshot_open=100.05,
                    final_raw_open=100.0,
                )
            )
    return sessions, rows


class TDXQualificationTests(unittest.TestCase):
    def test_exact_freshness_and_open_error_boundaries_pass(self) -> None:
        sessions, rows = _tdx_evidence()
        total = sum(item.expected_poll_slots for item in rows)
        permitted_stale = int(total * 0.005)
        for index in range(permitted_stale):
            row_index = index % len(rows)
            rows[row_index] = replace(
                rows[row_index],
                fresh_poll_slots=rows[row_index].fresh_poll_slots - 1,
            )
        # $100 uses the 5 bp threshold ($0.05); equality must pass.
        decision = evaluate_tdx_quote_qualification(rows, sessions)

        self.assertTrue(decision.qualified, decision.failures)
        self.assertAlmostEqual(float(decision.metrics["fresh_quote_ratio"]), 0.995)

    def test_one_open_outside_tolerance_fails_closed(self) -> None:
        sessions, rows = _tdx_evidence()
        rows[0] = replace(rows[0], snapshot_open=100.050001)
        decision = evaluate_tdx_quote_qualification(rows, sessions)

        self.assertFalse(decision.qualified)
        self.assertEqual(decision.status, "PAPER_BLOCKED")
        self.assertIn("opening_accuracy", decision.failures)

    def test_future_source_timestamp_and_nineteen_days_fail(self) -> None:
        sessions, rows = _tdx_evidence(19)
        rows[0] = replace(
            rows[0],
            opening_source_at=rows[0].opening_observed_at + timedelta(seconds=1),
        )
        decision = evaluate_tdx_quote_qualification(rows, sessions)

        self.assertFalse(decision.qualified)
        self.assertIn("twenty_consecutive_xnys_sessions", decision.failures)
        self.assertIn("timestamp_and_market_state_integrity", decision.failures)


if __name__ == "__main__":
    unittest.main()
