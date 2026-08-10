from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path
from typing import Any

from .backtest_engine import BacktestService
from .config import PlatformConfig
from .storage import Database
from .strategy_lab import ALLOWED_HOOKS, GENERATED_CLASS_NAME, validate_generated_source
from .strategies.course49_v3 import Course49V3Strategy


def load_strategy(path: Path) -> Course49V3Strategy:
    source = path.read_text(encoding="utf-8")
    validate_generated_source(source)
    spec = importlib.util.spec_from_file_location(f"strategy_lab_{hashlib.sha256(source.encode()).hexdigest()[:12]}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Cannot load generated strategy")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    strategy_type = getattr(module, GENERATED_CLASS_NAME)
    if not issubclass(strategy_type, Course49V3Strategy):
        raise ValueError("Generated class is not a V3 strategy")
    for name in ALLOWED_HOOKS:
        if name in strategy_type.__dict__:
            generated = list(inspect.signature(strategy_type.__dict__[name]).parameters)
            expected = list(inspect.signature(getattr(Course49V3Strategy, name)).parameters)
            if generated != expected:
                raise ValueError(f"Hook signature mismatch for {name}: expected {expected}")
    return strategy_type()


def validate(path: Path) -> dict[str, Any]:
    strategy = load_strategy(path)
    allowed = strategy.candidate_allowed(2, 0.8, {"EARLY_SEAL"}, "")
    score = strategy.candidate_score(
        board_quality=0.8,
        streak=2,
        continuation_rate=0.6,
        capital_score=0.5,
        first_limit_score=0.7,
        historical_premium=0.5,
    )
    weight = strategy.target_weight(0.15, 0.8, 0.8)
    if not isinstance(allowed, bool) or not isinstance(score, float) or not isinstance(weight, float):
        raise ValueError("Generated hook returned an invalid type")
    if not 0 <= score <= 2 or not 0 <= weight <= 1:
        raise ValueError("Generated hook returned an unsafe numeric range")
    return {"contract_tests": True, "future_data_access": False}


def replay(path: Path, baseline_id: str, experiment_id: str) -> dict[str, Any]:
    strategy = load_strategy(path)
    config = PlatformConfig()
    database = Database(config)
    database.initialize()
    service = BacktestService(config, database)
    service.strategies["course49_v3"] = strategy
    candidate = service.replay_course49(baseline_id, strategy_id="course49_v3")
    stress = service.replay_course49(
        baseline_id,
        strategy_id="course49_v3",
        execution_cost_multiplier=2.0,
    )
    source_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    for result in (candidate, stress):
        rows = database.query("SELECT parameters_json FROM backtests WHERE backtest_id=?", (result["backtest_id"],))
        parameters = json.loads(str(rows[0]["parameters_json"] or "{}"))
        parameters.update({"experiment_id": experiment_id, "generated_source_hash": source_hash})
        database.execute(
            "UPDATE backtests SET parameters_json=? WHERE backtest_id=?",
            (json.dumps(parameters, ensure_ascii=False), result["backtest_id"]),
        )
    return {
        "candidate": {"backtest_id": candidate["backtest_id"], "metrics": candidate},
        "stress": {"backtest_id": stress["backtest_id"], "metrics": stress},
    }


def main(arguments: list[str] | None = None) -> int:
    args = arguments or sys.argv[1:]
    if len(args) < 2:
        raise SystemExit("usage: strategy_lab_runner validate|replay SOURCE [BASELINE EXPERIMENT]")
    action, source = args[0], Path(args[1]).resolve()
    if action == "validate":
        result = validate(source)
    elif action == "replay" and len(args) == 4:
        result = replay(source, args[2], args[3])
    else:
        raise SystemExit("invalid strategy lab action")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
