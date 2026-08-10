from __future__ import annotations

import ast
import importlib.util
import hashlib
import inspect
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .config import PlatformConfig
from .storage import Database
from .strategies.course49_v3 import Course49V3Strategy


EXPERIMENT_PROMPT_VERSION = "course49-v3-hooks-v1"
GENERATED_CLASS_NAME = "GeneratedCourse49V3"
ALLOWED_HOOKS = frozenset({"candidate_allowed", "select_mode", "candidate_score", "target_weight"})
FORBIDDEN_NAMES = frozenset(
    {
        "open", "eval", "exec", "compile", "__import__", "globals", "locals", "vars",
        "getattr", "setattr", "delattr", "hasattr", "type", "dir", "object",
        "breakpoint", "input", "help", "os", "sys",
        "subprocess", "socket", "pathlib", "importlib", "builtins", "requests", "httpx",
    }
)


class ExperimentGeneration(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    source_code: str = Field(min_length=80, max_length=20_000)


def validate_generated_source(source: str) -> dict[str, Any]:
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)]
    allowed_top = (ast.ImportFrom, ast.ClassDef)
    if any(not isinstance(node, allowed_top) for node in tree.body):
        raise ValueError("Generated module may only contain one approved import and one class")
    imports = [node for node in tree.body if isinstance(node, ast.ImportFrom)]
    if len(imports) != 1 or imports[0].module != "research_platform.strategies.course49_v3":
        raise ValueError("Generated module must import the approved V3 base class")
    if {alias.name for alias in imports[0].names} != {"Course49V3Strategy"}:
        raise ValueError("Only Course49V3Strategy may be imported")
    if len(classes) != 1 or classes[0].name != GENERATED_CLASS_NAME:
        raise ValueError(f"Generated module must define exactly {GENERATED_CLASS_NAME}")
    class_node = classes[0]
    if len(class_node.bases) != 1 or not isinstance(class_node.bases[0], ast.Name) or class_node.bases[0].id != "Course49V3Strategy":
        raise ValueError("Generated strategy must inherit Course49V3Strategy")
    methods = [node for node in class_node.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if not methods or any(isinstance(node, ast.AsyncFunctionDef) for node in methods):
        raise ValueError("Generated strategy must override at least one synchronous hook")
    method_names = {node.name for node in methods}
    if not method_names.issubset(ALLOWED_HOOKS):
        raise ValueError(f"Only these hooks may be overridden: {', '.join(sorted(ALLOWED_HOOKS))}")
    for node in class_node.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if not isinstance(node, ast.FunctionDef):
            raise ValueError("Generated class may only contain hook methods")
        if node.decorator_list:
            raise ValueError("Generated hooks may not use decorators")
    forbidden_nodes = (
        ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith, ast.Await, ast.Yield, ast.YieldFrom,
        ast.Try, ast.Raise, ast.Delete, ast.NamedExpr, ast.Import, ast.Lambda,
    )
    for node in ast.walk(tree):
        if isinstance(node, forbidden_nodes):
            raise ValueError(f"Forbidden syntax: {type(node).__name__}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden name: {node.id}")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("Dunder attribute access is forbidden")
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "self":
            raise ValueError("Generated hooks may not access arbitrary strategy attributes")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_NAMES:
            raise ValueError(f"Forbidden call: {node.func.id}")
    return {"class_name": GENERATED_CLASS_NAME, "overridden_hooks": sorted(method_names)}


class StrategyLabService:
    def __init__(
        self,
        config: PlatformConfig,
        database: Database,
        *,
        client: Any | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self._provided_client = client

    def create_experiment(self, baseline_backtest_id: str, hypothesis: str) -> dict[str, Any]:
        baseline_rows = self.database.query(
            "SELECT * FROM backtests WHERE backtest_id=? AND status='SUCCEEDED'",
            (baseline_backtest_id,),
        )
        if not baseline_rows:
            raise ValueError("Baseline backtest must exist and be SUCCEEDED")
        baseline = baseline_rows[0]
        if str(baseline["strategy_id"]) != "course49_v3" or not baseline.get("snapshot_id"):
            raise ValueError("Strategy Lab requires a snapshot-backed course49_v3 baseline")
        experiment_id = uuid4().hex
        now = datetime.now().astimezone().isoformat()
        self.database.execute(
            """INSERT INTO strategy_experiments
            (experiment_id, base_strategy_id, baseline_backtest_id, hypothesis, status,
             created_at, prompt_version, baseline_metrics_json)
            VALUES (?, 'course49_v3', ?, ?, 'GENERATING', ?, ?, ?)""",
            (
                experiment_id,
                baseline_backtest_id,
                hypothesis.strip(),
                now,
                EXPERIMENT_PROMPT_VERSION,
                str(baseline.get("metrics_json") or "{}"),
            ),
        )
        try:
            generation, response = self._generate(hypothesis)
            validation = validate_generated_source(generation.source_code)
            directory = self.config.strategy_lab_dir / "experiments" / experiment_id
            directory.mkdir(parents=True, exist_ok=False)
            source_path = directory / "strategy.py"
            source_path.write_text(generation.source_code, encoding="utf-8", newline="\n")
            source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
            contract = self._run_child("validate", source_path, timeout_seconds=120)
            validation.update(contract)
            replay = self._run_child(
                "replay",
                source_path,
                baseline_backtest_id,
                experiment_id,
                timeout_seconds=1_200,
            )
            baseline_metrics = json.loads(str(baseline.get("metrics_json") or "{}"))
            candidate_metrics = replay["candidate"]["metrics"]
            stress_metrics = replay["stress"]["metrics"]
            gates = self._comparison_gates(
                baseline_metrics,
                candidate_metrics,
                stress_metrics,
                str(baseline.get("snapshot_id") or ""),
            )
            gates["contract_tests"] = bool(validation.get("contract_tests"))
            gates["future_data_test"] = validation.get("future_data_access") is False
            validation["gates"] = gates
            validation["passed"] = all(gates.values())
            from .validation import validate_course49_v3

            validation["statistical_validation"] = validate_course49_v3(
                self.database,
                replay["candidate"]["backtest_id"],
                stress_backtest_id=replay["stress"]["backtest_id"],
            )
            artifact_id = uuid4().hex
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE strategy_experiments SET status=?, finished_at=?, model=?, response_id=?,
                    source_path=?, source_hash=?, summary=?, validation_json=?, candidate_metrics_json=?,
                    stress_metrics_json=?, candidate_backtest_id=?, stress_backtest_id=?, error=''
                    WHERE experiment_id=?""",
                    (
                        "READY" if validation["passed"] else "REJECTED",
                        datetime.now().astimezone().isoformat(),
                        str(getattr(response, "model", self.config.openai_model)),
                        str(getattr(response, "id", "")),
                        str(source_path),
                        source_hash,
                        generation.summary,
                        json.dumps(validation, ensure_ascii=False),
                        json.dumps(candidate_metrics, ensure_ascii=False),
                        json.dumps(stress_metrics, ensure_ascii=False),
                        replay["candidate"]["backtest_id"],
                        replay["stress"]["backtest_id"],
                        experiment_id,
                    ),
                )
                connection.execute(
                    """INSERT INTO experiment_artifacts
                    (artifact_id, experiment_id, artifact_type, path, content_hash, metadata_json, created_at)
                    VALUES (?, ?, 'SOURCE', ?, ?, ?, ?)""",
                    (
                        artifact_id,
                        experiment_id,
                        str(source_path),
                        source_hash,
                        json.dumps({"validation": validation}, ensure_ascii=False),
                        datetime.now().astimezone().isoformat(),
                    ),
                )
        except Exception as exc:
            self.database.execute(
                "UPDATE strategy_experiments SET status='FAILED', finished_at=?, error=? WHERE experiment_id=?",
                (datetime.now().astimezone().isoformat(), str(exc), experiment_id),
            )
        return self.get_experiment(experiment_id)

    def promote(self, experiment_id: str) -> dict[str, Any]:
        experiment = self.get_experiment(experiment_id)
        if experiment["status"] != "READY" or not experiment.get("validation", {}).get("passed"):
            raise ValueError("Only a READY experiment that passed all gates can be promoted")
        source = Path(str(experiment["source_path"]))
        if not source.is_file():
            raise ValueError("Experiment source is missing")
        if hashlib.sha256(source.read_bytes()).hexdigest() != experiment["source_hash"]:
            raise ValueError("Experiment source hash mismatch")
        strategy_id = f"course49_v3_lab_{experiment_id[:8]}"
        destination = self.config.strategy_lab_dir / "promoted" / strategy_id
        destination.mkdir(parents=True, exist_ok=False)
        copied = destination / "strategy.py"
        shutil.copy2(source, copied)
        manifest = {
            "strategy_id": strategy_id,
            "base_strategy_id": "course49_v3",
            "experiment_id": experiment_id,
            "source_hash": experiment["source_hash"],
            "lifecycle": "PROMOTED",
            "scan_enabled": False,
            "backtest_enabled": True,
            "promoted_at": datetime.now().astimezone().isoformat(),
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self.database.execute(
            """UPDATE strategy_experiments SET status='PROMOTED', promoted_strategy_id=?
            WHERE experiment_id=?""",
            (strategy_id, experiment_id),
        )
        return self.get_experiment(experiment_id)

    def list_experiments(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._decode(item) for item in self.database.query(
            "SELECT * FROM strategy_experiments ORDER BY created_at DESC LIMIT ?", (limit,)
        )]

    def get_experiment(self, experiment_id: str) -> dict[str, Any]:
        rows = self.database.query(
            "SELECT * FROM strategy_experiments WHERE experiment_id=?", (experiment_id,)
        )
        if not rows:
            raise KeyError(experiment_id)
        result = self._decode(rows[0])
        result["artifacts"] = [self._decode(item) for item in self.database.query(
            "SELECT * FROM experiment_artifacts WHERE experiment_id=? ORDER BY created_at",
            (experiment_id,),
        )]
        return result

    def _generate(self, hypothesis: str) -> tuple[ExperimentGeneration, Any]:
        if not self.config.openai_api_key and self._provided_client is None:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        client = self._provided_client or self._create_client()
        contracts = {
            name: str(inspect.signature(getattr(Course49V3Strategy, name)))
            for name in sorted(ALLOWED_HOOKS)
        }
        error = ""
        for attempt in range(2):
            response = client.responses.parse(
                model=self.config.openai_model,
                store=False,
                reasoning={"effort": "medium"},
                input=[
                    {
                        "role": "system",
                        "content": (
                            "生成一个受限的 course49_v3 实验变体。source_code 必须只包含 "
                            "`from research_platform.strategies.course49_v3 import Course49V3Strategy` "
                            "和 `class GeneratedCourse49V3(Course49V3Strategy)`。只能覆盖给定 hook，"
                            "不得导入其他模块、访问文件网络进程数据库、覆盖 scan 或生成解释性顶层代码。"
                            + (f" 上次源码校验失败：{error}。请完整重试。" if error else "")
                        ),
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {"hypothesis": hypothesis, "approved_hook_signatures": contracts},
                            ensure_ascii=False,
                        ),
                    },
                ],
                text_format=ExperimentGeneration,
            )
            output = response.output_parsed
            try:
                if output is None:
                    raise ValueError("Model returned no experiment source")
                validate_generated_source(output.source_code)
                return output, response
            except (SyntaxError, ValueError) as exc:
                error = str(exc)
                if attempt == 1:
                    raise
        raise RuntimeError("Experiment generation failed")

    def _create_client(self) -> Any:
        from openai import OpenAI

        return OpenAI(
            api_key=self.config.openai_api_key,
            timeout=self.config.openai_timeout_seconds,
            max_retries=self.config.openai_max_retries,
        )

    def _run_child(
        self,
        action: str,
        source_path: Path,
        *arguments: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        command = [
            sys.executable,
            "-m",
            "research_platform.strategy_lab_runner",
            action,
            str(source_path),
            *arguments,
        ]
        environment = os.environ.copy()
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("OPENAI_MODEL", None)
        environment["PYTHONUTF8"] = "1"
        process = subprocess.Popen(
            command,
            cwd=self.config.repository_root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        started = time.monotonic()
        peak = 0
        try:
            import psutil

            tracked = psutil.Process(process.pid)
            while process.poll() is None:
                try:
                    memory = tracked.memory_info().rss + sum(
                        child.memory_info().rss for child in tracked.children(recursive=True)
                    )
                except psutil.Error:
                    memory = 0
                peak = max(peak, memory)
                if peak > 2_500_000_000:
                    process.kill()
                    raise RuntimeError("Experiment memory limit exceeded 2.5 GB")
                if time.monotonic() - started > timeout_seconds:
                    process.kill()
                    raise RuntimeError(f"Experiment timed out after {timeout_seconds} seconds")
                time.sleep(0.1)
            stdout, stderr = process.communicate()
        finally:
            if process.poll() is None:
                process.kill()
        if process.returncode != 0:
            raise RuntimeError((stderr or stdout or "Experiment child failed").strip())
        result = json.loads(stdout)
        result["peak_memory_bytes"] = peak
        return result

    @staticmethod
    def _comparison_gates(
        baseline: dict[str, Any],
        candidate: dict[str, Any],
        stress: dict[str, Any],
        baseline_snapshot_id: str,
    ) -> dict[str, bool]:
        return {
            "candidate_completed": bool(candidate),
            "same_snapshot_replay": bool(baseline_snapshot_id)
            and candidate.get("snapshot_id") == baseline_snapshot_id
            and stress.get("snapshot_id") == baseline_snapshot_id,
            "double_cost_replay": float(
                (stress.get("parameters") or {}).get("execution_cost_multiplier", 0)
            ) >= 2.0,
            "total_return_not_lower": float(candidate.get("total_return", -1))
            >= float(baseline.get("total_return", 0)),
            "drawdown_within_one_point": float(candidate.get("max_drawdown", -1))
            >= float(baseline.get("max_drawdown", 0)) - 0.01,
            "double_cost_positive": float(stress.get("total_return", -1)) > 0,
        }

    @staticmethod
    def _decode(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json"):
                try:
                    result[key.removesuffix("_json")] = json.loads(str(result[key] or "{}"))
                except json.JSONDecodeError:
                    result[key.removesuffix("_json")] = result[key]
        return result


def load_promoted_strategies(config: PlatformConfig) -> dict[str, Course49V3Strategy]:
    root = config.strategy_lab_dir / "promoted"
    if not root.exists():
        return {}
    result: dict[str, Course49V3Strategy] = {}
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_path = manifest_path.parent / "strategy.py"
            source = source_path.read_text(encoding="utf-8")
            if hashlib.sha256(source_path.read_bytes()).hexdigest() != manifest["source_hash"]:
                continue
            validate_generated_source(source)
            spec = importlib.util.spec_from_file_location(
                f"promoted_{manifest['strategy_id']}", source_path
            )
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            strategy = getattr(module, GENERATED_CLASS_NAME)()
            strategy.metadata = replace(
                strategy.metadata,
                strategy_id=str(manifest["strategy_id"]),
                version=f"3.0.0-lab.{str(manifest['experiment_id'])[:8]}",
                name=f"V3 实验 {str(manifest['experiment_id'])[:8]}",
                description="人工晋级的 V3 实验变体，仅允许回测。",
                strategy_family="course49_v3",
                lifecycle="PROMOTED",
                scan_enabled=False,
                backtest_enabled=True,
                enabled=True,
            )
            result[str(manifest["strategy_id"])] = strategy
        except (OSError, KeyError, TypeError, ValueError, SyntaxError):
            continue
    return result
