from __future__ import annotations

import tempfile
import unittest
import hashlib
import json
from datetime import datetime
from pathlib import Path

from research_platform.service import PlatformService
from research_platform.storage import Database
from research_platform.strategy_lab import StrategyLabService, load_promoted_strategies, validate_generated_source
from research_platform.strategy_lab_runner import validate
from research_platform.tests.helpers import temporary_config


SAFE_SOURCE = """from research_platform.strategies.course49_v3 import Course49V3Strategy

class GeneratedCourse49V3(Course49V3Strategy):
    def candidate_score(self, *, board_quality, streak, continuation_rate, capital_score, first_limit_score, historical_premium):
        return super().candidate_score(board_quality=board_quality, streak=streak, continuation_rate=continuation_rate, capital_score=capital_score, first_limit_score=first_limit_score, historical_premium=historical_premium) + 0.1
"""


class StrategyLabTests(unittest.TestCase):
    def test_only_approved_hooks_are_allowed(self) -> None:
        result = validate_generated_source(SAFE_SOURCE)
        self.assertEqual(result["overridden_hooks"], ["candidate_score"])

    def test_file_and_scan_access_are_rejected(self) -> None:
        dangerous = SAFE_SOURCE.replace(
            "def candidate_score(self, *, board_quality, streak, continuation_rate, capital_score, first_limit_score, historical_premium):\n"
            "        return super().candidate_score(board_quality=board_quality, streak=streak, continuation_rate=continuation_rate, capital_score=capital_score, first_limit_score=first_limit_score, historical_premium=historical_premium) + 0.1",
            "def scan(self, context):\n        return open('stolen.txt').read()",
        )
        with self.assertRaises(ValueError):
            validate_generated_source(dangerous)

    def test_extra_import_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "approved import"):
            validate_generated_source("import os\n" + SAFE_SOURCE)

    def test_child_contract_loader_executes_approved_hook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strategy.py"
            path.write_text(SAFE_SOURCE, encoding="utf-8")
            result = validate(path)
        self.assertTrue(result["contract_tests"])
        self.assertFalse(result["future_data_access"])

    def test_promoted_strategy_is_registered_backtest_only_and_scan_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = temporary_config(Path(directory))
            database = Database(config)
            database.initialize()
            now = datetime.now().astimezone().isoformat()
            database.execute(
                """INSERT INTO backtests
                (backtest_id, strategy_id, status, started_at, finished_at, snapshot_id, metrics_json)
                VALUES ('baseline', 'course49_v3', 'SUCCEEDED', ?, ?, 'snapshot', '{}')""",
                (now, now),
            )
            source_dir = config.strategy_lab_dir / "experiments" / "experiment123"
            source_dir.mkdir(parents=True)
            source = source_dir / "strategy.py"
            source.write_text(SAFE_SOURCE, encoding="utf-8")
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            database.execute(
                """INSERT INTO strategy_experiments
                (experiment_id, base_strategy_id, baseline_backtest_id, hypothesis, status,
                 created_at, prompt_version, source_path, source_hash, validation_json)
                VALUES ('experiment123', 'course49_v3', 'baseline', '测试晋级约束', 'READY',
                        ?, 'test', ?, ?, ?)""",
                (now, str(source), source_hash, json.dumps({"passed": True})),
            )

            promoted = StrategyLabService(config, database).promote("experiment123")
            strategies = load_promoted_strategies(config)
            strategy_id = str(promoted["promoted_strategy_id"])

            self.assertIn(strategy_id, strategies)
            self.assertFalse(strategies[strategy_id].metadata.scan_enabled)
            self.assertTrue(strategies[strategy_id].metadata.backtest_enabled)
            service = PlatformService(config)
            with self.assertRaisesRegex(ValueError, "not enabled for scanning"):
                service.run_scan([strategy_id])


if __name__ == "__main__":
    unittest.main()
