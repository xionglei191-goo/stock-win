from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from research_platform.backtest_engine import BacktestService
from research_platform.models import (
    DataRequirement,
    ExecutionModel,
    PlatformSignal,
    RuntimeAdapter,
    SignalStatus,
    StrategyMetadata,
    StrategyScanResult,
)
from research_platform.plugin_loader import load_strategy_registry
from research_platform.storage import Database
from research_platform.strategies.pairs_arbitrage import PairSpec, PairsArbitrageStrategy
from research_platform.tests.helpers import temporary_config


class GenericSignalStrategy:
    metadata = StrategyMetadata(
        strategy_id="generic_signal_test",
        version="1.0.0",
        name="generic signal test",
        description="test",
        frequency="1d",
        requires_approval=False,
        runtime_adapter=RuntimeAdapter.GENERIC_DAILY,
        data_requirements=(
            DataRequirement("bars", "1d", "front", 2, True, ("Close",)),
            DataRequirement("bars", "1d", "none", 2, True, ("Open", "Close")),
        ),
    )

    @property
    def required_codes(self) -> tuple[str, ...]:
        return ("600000.SH",)

    def scan(
        self,
        *,
        run_id: str,
        asof: pd.Timestamp,
        raw_bars: dict[str, pd.DataFrame],
        positions: list[dict[str, Any]],
        **_: Any,
    ) -> StrategyScanResult:
        frame = raw_bars.get("600000.SH")
        if frame is None or len(frame) < 2:
            raise ValueError("warmup")
        generated = pd.Timestamp(asof).replace(hour=18).to_pydatetime().astimezone()
        next_open = generated + timedelta(hours=15, minutes=30)
        side = "SELL" if positions else "BUY"
        signal = PlatformSignal(
            run_id=run_id,
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            generated_at=generated,
            available_at=next_open,
            code="600000.SH",
            side=side,
            strength=0.8,
            target_weight=0.2 if side == "BUY" else 0.0,
            horizon="1d",
            valid_until=next_open + timedelta(hours=6),
            stop_price=8.0 if side == "BUY" else None,
            status=SignalStatus.APPROVED,
            reason_codes=("GENERIC_TEST",),
        )
        return StrategyScanResult(
            strategy=self.metadata,
            signals=(signal,),
            candidates=(),
            state={"asof": pd.Timestamp(asof).date().isoformat()},
        )


def bars(values: np.ndarray, index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": values,
            "High": values * 1.01,
            "Low": values * 0.99,
            "Close": values,
            "Volume": np.full(len(values), 1_000_000),
            "Amount": values * 1_000_000,
        },
        index=index,
    )


class PluginRuntimeTests(unittest.TestCase):
    def test_local_manifest_plugin_is_discovered_and_bad_plugin_is_isolated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            valid = config.strategy_plugin_dir / "local_pairs"
            valid.mkdir(parents=True)
            (valid / "plugin.json").write_text(
                json.dumps(
                    {
                        "strategy_id": "local_pairs_v1",
                        "api_version": "1",
                        "entrypoint": "strategy.py:create_strategy",
                    }
                ),
                encoding="utf-8",
            )
            (valid / "strategy.py").write_text(
                "from dataclasses import replace\n"
                "from research_platform.strategies.pairs_arbitrage import PairsArbitrageStrategy\n"
                "def create_strategy():\n"
                "    strategy = PairsArbitrageStrategy()\n"
                "    strategy.metadata = replace(strategy.metadata, strategy_id='local_pairs_v1', version='1.0.0')\n"
                "    return strategy\n",
                encoding="utf-8",
            )
            invalid = config.strategy_plugin_dir / "broken"
            invalid.mkdir(parents=True)
            (invalid / "plugin.json").write_text(
                json.dumps(
                    {
                        "strategy_id": "broken_v1",
                        "api_version": "99",
                        "entrypoint": "strategy.py:create_strategy",
                    }
                ),
                encoding="utf-8",
            )

            strategies, issues = load_strategy_registry(config)

            self.assertIn("local_pairs_v1", strategies)
            self.assertEqual(
                getattr(strategies["local_pairs_v1"], "__plugin_origin__"),
                "local:local_pairs",
            )
            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].plugin_id, "broken_v1")

    def test_generic_single_leg_strategy_uses_shared_execution_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = BacktestService(config, database)
            strategy = GenericSignalStrategy()
            service.strategies[strategy.metadata.strategy_id] = strategy
            index = pd.date_range("2026-01-05", periods=30, freq="B")
            frame = bars(np.linspace(10.0, 10.7, len(index)), index)

            result = service._run_generic_signal_strategy(
                "generic_single",
                strategy.metadata.strategy_id,
                {"600000.SH": "test"},
                {"600000.SH": frame},
                {"600000.SH": frame},
                frame,
                {},
                {},
                None,
                None,
                capital_weight=0.5,
                execution_config=config.portfolio,
            )

            self.assertGreaterEqual(result["metrics"]["trades"], 2)
            self.assertEqual(result["metrics"]["execution_model"], "SINGLE_LEG")
            persisted = database.query(
                "SELECT DISTINCT strategy_id FROM backtest_trades WHERE backtest_id=?",
                ("generic_single",),
            )
            self.assertEqual(persisted, [{"strategy_id": strategy.metadata.strategy_id}])

    def test_any_generic_multileg_strategy_uses_atomic_group_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            service = BacktestService(config, database)
            pair = PairSpec("LEFT.SH", "RIGHT.SH", "test")
            strategy = PairsArbitrageStrategy((pair,))
            strategy.metadata = replace(
                strategy.metadata,
                strategy_id="custom_arbitrage_v1",
                version="1.0.0",
            )
            strategy.entry_zscore = 0.0
            strategy.exit_zscore = -1.0
            strategy.stop_zscore = 99.0
            strategy.minimum_correlation = 0.0
            service.strategies[strategy.metadata.strategy_id] = strategy
            index = pd.date_range("2025-01-01", periods=90, freq="B")
            right = np.linspace(9.0, 11.0, len(index))
            left = right * (1.05 + np.sin(np.arange(len(index))) * 0.002)
            left_frame = bars(left, index)
            right_frame = bars(right, index)

            result = service._run_group_intent_strategy(
                "generic_group",
                strategy.metadata.strategy_id,
                {pair.left: "left", pair.right: "right"},
                {pair.left: left_frame, pair.right: right_frame},
                {pair.left: left_frame, pair.right: right_frame},
                left_frame,
                {},
                {},
                None,
                None,
                capital_weight=0.5,
                execution_config=config.portfolio,
            )

            sides = set(result["trades"].get("side", pd.Series(dtype=str)))
            self.assertEqual(sides, {"BUY", "SHORT"})
            self.assertTrue(result["metrics"]["atomic_execution"])
            self.assertEqual(result["metrics"]["execution_model"], "MULTI_LEG")


if __name__ == "__main__":
    unittest.main()
