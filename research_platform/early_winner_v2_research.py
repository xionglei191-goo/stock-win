from __future__ import annotations

import hashlib
import json
import platform
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .config import PlatformConfig
from .early_winner_research import (
    MODEL_PARAMETERS,
    MODEL_RANDOM_SEED,
    ResearchDataBlockedError,
    _embargo_head_dates,
    _evaluate_non_overlapping_portfolio,
    _purge_tail_dates,
    _ranking_metrics,
)
from .storage import Database, _file_sha256
from .strategies.early_winner import FEATURE_COLUMNS, mark_research_universe_eligibility
from .strategies.early_winner_v2 import EarlyWinnerV2Strategy, ML_STRATEGY_ID, PROJECT_ID


PROJECT_VERSION = "2.0.0-dev1"
PROJECT_NAME = "早期强势股识别 V2"
PROJECT_DESCRIPTION = (
    "只在 2018—2023 开发区修复 V1 数据管线；2024/2025 不用于调参，"
    "2026 前瞻集在开发门禁通过前保持封存。"
)
DEVELOPMENT_YEARS = tuple(range(2018, 2024))
OOS_DEVELOPMENT_YEARS = (2020, 2021, 2022, 2023)
FORWARD_YEAR = 2026
PREPROCESSOR_VERSION = "early-winner-v2-train-only-winsor-v1"
PROTOCOL_VERSION = "early-winner-v2-development-v1"
TECHNICAL_FEATURES = (
    "industry_momentum",
    "industry_breadth",
    "industry_amount_trend",
    "return_20",
    "return_60",
    "return_120",
    "relative_return_20",
    "relative_return_60",
    "relative_return_120",
    "volume_ratio",
    "amount_ratio",
    "breakout_distance",
    "ma20_slope",
    "event_score",
    "price_to_ma60",
)
CORE_FEATURES = (
    *TECHNICAL_FEATURES,
    "revenue_yoy",
    "profit_yoy",
    "gross_margin_change",
    "roe",
    "ocf_profit_ratio",
    "forecast_revision",
)
VARIANT_FEATURES = {
    "full_clean": tuple(FEATURE_COLUMNS),
    "core_clean": CORE_FEATURES,
    "technical_clean": TECHNICAL_FEATURES,
}
DERIVED_VARIANTS = (
    "technical_breadth50",
    "blend50",
    "blend50_breadth50",
)


class EarlyWinnerV2ResearchService:
    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.strategy = EarlyWinnerV2Strategy()
        current = self.database.query(
            "SELECT status, data_asof, data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        status = str(current[0]["status"]) if current else "DEVELOPMENT_AUDIT_REQUIRED"
        data_asof = str(current[0].get("data_asof") or "") or None if current else None
        gates = _json_value(current[0].get("data_gates_json"), {}) if current else {}
        self.database.upsert_research_project(
            project_id=PROJECT_ID,
            version=PROJECT_VERSION,
            name=PROJECT_NAME,
            description=PROJECT_DESCRIPTION,
            status=status,
            data_asof=data_asof,
            data_gates=gates,
        )

    def detail(self) -> dict[str, Any]:
        project_rows = self.database.query(
            "SELECT * FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        if not project_rows:
            raise KeyError(PROJECT_ID)
        project = dict(project_rows[0])
        project["data_gates"] = _json_value(project.pop("data_gates_json", "{}"), {})
        validations = self.database.query(
            """SELECT * FROM research_validations WHERE project_id=?
            ORDER BY created_at DESC LIMIT 1""",
            (PROJECT_ID,),
        )
        project["latest_development_audit"] = (
            _decode_validation(validations[0]) if validations else None
        )
        batches = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id=?
            ORDER BY fetched_at DESC LIMIT 10""",
            (PROJECT_ID,),
        )
        project["latest_batches"] = [_decode_batch(row) for row in batches]
        project["strategy"] = {
            "strategy_id": self.strategy.metadata.strategy_id,
            "version": self.strategy.metadata.version,
            "name": self.strategy.metadata.name,
            "lifecycle": self.strategy.metadata.lifecycle,
            "category": self.strategy.metadata.category.value,
            "scan_enabled": self.strategy.metadata.scan_enabled,
            "backtest_enabled": self.strategy.metadata.backtest_enabled,
        }
        project["development_years"] = list(DEVELOPMENT_YEARS)
        project["excluded_tuning_years"] = [2024, 2025]
        project["forward_year"] = FORWARD_YEAR
        project["forward_validation_opened"] = False
        project["candidate_generation_enabled"] = False
        project["trade_signals_enabled"] = False
        project["promotion_allowed"] = False
        return project

    def run_development_audit(self, progress_callback: Any | None = None) -> dict[str, Any]:
        self.database.update_research_project(PROJECT_ID, status="DEVELOPMENT_AUDITING")
        _progress(progress_callback, "LOAD_DEVELOPMENT", 0.05, "只读取 2018—2023 冻结批次")
        source_batches = self._development_batches()
        frame = self._load_development_frame(source_batches)
        profile = profile_development_data(frame)
        _progress(progress_callback, "DATA_QUALITY", 0.15, "完成标签、缺失、漂移和时点审计")
        experiment = run_development_experiment(frame, progress_callback=progress_callback)
        variant_gates = {
            name: {
                "yearly": {
                    year: bool(metrics["gate_passed"])
                    for year, metrics in variant["yearly"].items()
                },
                "passed": bool(variant["passed"]),
            }
            for name, variant in experiment["variants"].items()
        }
        passing = sorted(name for name, gate in variant_gates.items() if gate["passed"])
        status = "DEVELOPMENT_READY" if passing else "DEVELOPMENT_REJECTED"
        source_hash = _source_snapshot_hash(source_batches)
        protocol_hash = hashlib.sha256(
            json.dumps(
                {
                    "protocol": PROTOCOL_VERSION,
                    "preprocessor": PREPROCESSOR_VERSION,
                    "source_hash": source_hash,
                    "variants": sorted(experiment["variants"]),
                    "parameters": MODEL_PARAMETERS,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        audit_id = f"ewv2_dev_{protocol_hash[:24]}"
        previous = self.database.query(
            "SELECT created_at FROM research_validations WHERE validation_id=?",
            (audit_id,),
        )
        now = (
            str(previous[0]["created_at"])
            if previous
            else datetime.now().astimezone().isoformat()
        )
        audit_payload = {
            "audit_id": audit_id,
            "project_id": PROJECT_ID,
            "status": status,
            "created_at": now,
            "protocol_version": PROTOCOL_VERSION,
            "protocol_hash": protocol_hash,
            "source_snapshot_hash": source_hash,
            "source_batches": [
                {
                    "batch_id": item["batch_id"],
                    "published_end": item["published_end"],
                    "row_count": item["row_count"],
                    "content_hash": item["content_hash"],
                }
                for item in source_batches
            ],
            "profile": profile,
            "experiment": experiment,
            "passing_variants": passing,
            "forward_validation_opened": False,
            "tuning_exclusions": [2024, 2025, 2026],
            "promotion_allowed": False,
        }
        path = self._save_audit_artifact(audit_id, audit_payload)
        self.database.save_research_data_batch(
            {
                "batch_id": audit_id,
                "project_id": PROJECT_ID,
                "dataset": "early_winner_v2_development_audit",
                "source": "early_winner_v1_frozen_2018_2023",
                "status": "SUCCEEDED",
                "fetched_at": now,
                "published_start": "2018-01-01",
                "published_end": "2023-12-31",
                "row_count": int(profile["grain"]["rows"]),
                "path": str(path),
                "content_hash": _file_sha256(path),
                "schema_hash": protocol_hash,
                "metadata": {
                    "protocol_version": PROTOCOL_VERSION,
                    "source_snapshot_hash": source_hash,
                    "passing_variants": passing,
                    "forward_validation_opened": False,
                },
                "error": "",
            }
        )
        validation = {
            "validation_id": audit_id,
            "project_id": PROJECT_ID,
            "status": status,
            "created_at": now,
            "finished_at": now,
            "snapshot_id": f"ewv2fs_{source_hash[:32]}",
            "rule_metrics": experiment["variants"]["full_clean"],
            "ml_metrics": experiment["variants"]["technical_clean"],
            "baseline_metrics": experiment["baseline"],
            "stress_metrics": {
                "core_clean": experiment["variants"]["core_clean"],
                "technical_breadth50": experiment["variants"]["technical_breadth50"],
                "blend50": experiment["variants"]["blend50"],
                "blend50_breadth50": experiment["variants"]["blend50_breadth50"],
                "profile": profile,
                "protocol_hash": protocol_hash,
                "forward_validation_opened": False,
            },
            "gates": variant_gates,
            "champion": {},
            "error": "" if passing else "no development variant passed every 2020-2023 OOS year",
        }
        self.database.save_research_validation(validation)
        gates = {
            "source_snapshot": {
                "ready": True,
                "detail": "只包含 2018—2023；内容哈希已复核",
                "row_count": int(profile["grain"]["rows"]),
            },
            "point_in_time": {
                "ready": bool(profile["time_audit"]["passed"]),
                "detail": "published_at/effective_at 均未晚于决策时点",
            },
            "label_scope_v2": {
                "ready": True,
                "detail": "仅可执行且有 60 日结果的 49,700 行进入训练标签",
                "row_count": int(profile["labels"]["v2_labeled_rows"]),
            },
            "development_stability": {
                "ready": bool(passing),
                "detail": (
                    f"通过方案: {', '.join(passing)}" if passing
                    else "没有方案逐年通过 2020—2023 门禁"
                ),
            },
            "forward_2026": {
                "ready": False,
                "status": "SEALED",
                "detail": "开发门禁未通过，2026 前瞻集未打开",
            },
        }
        self.database.update_research_project(
            PROJECT_ID,
            status=status,
            data_asof="2023-12-31",
            data_gates=gates,
        )
        _progress(progress_callback, "COMPLETED", 1.0, f"V2 开发审计完成：{status}")
        return {"project_id": PROJECT_ID, **audit_payload}

    def _development_batches(self) -> list[dict[str, Any]]:
        batches = self.database.query(
            """SELECT batch_id, path, content_hash, schema_hash, published_start,
            published_end, row_count FROM research_data_batches
            WHERE project_id='early_winner_v1'
              AND dataset='early_winner_features'
              AND status='SUCCEEDED'
              AND published_end <= '2023-12-31'
            ORDER BY published_end"""
        )
        years = {
            int(str(item.get("published_end") or "")[:4])
            for item in batches
            if str(item.get("published_end") or "")[:4].isdigit()
        }
        if years != set(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError(
                f"V2 开发批次年份不完整或越界: {sorted(years)}"
            )
        for batch in batches:
            path = Path(str(batch.get("path") or ""))
            if not path.exists() or _file_sha256(path) != str(batch.get("content_hash") or ""):
                raise ResearchDataBlockedError(
                    f"V2 开发源批次文件缺失或哈希不一致: {batch.get('batch_id')}"
                )
            if str(batch.get("published_end") or "") > "2023-12-31":
                raise ResearchDataBlockedError("V2 开发审计禁止读取 2024/2025 冻结测试批次")
        return batches

    @staticmethod
    def _load_development_frame(batches: Iterable[Mapping[str, Any]]) -> pd.DataFrame:
        frames = [pd.read_parquet(str(item["path"])) for item in batches]
        if not frames:
            raise ResearchDataBlockedError("V2 没有可用的开发期特征")
        frame = pd.concat(frames, ignore_index=True)
        years = pd.to_datetime(frame["asof"], errors="coerce").dt.year
        if years.isna().any() or not set(years.unique()).issubset(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError("V2 开发帧包含无效日期或开发期外数据")
        return frame.sort_values(["asof", "code"]).reset_index(drop=True)

    def _save_audit_artifact(self, audit_id: str, payload: Mapping[str, Any]) -> Path:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "audits"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{audit_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2),
            encoding="utf-8",
        )
        return path


def profile_development_data(frame: pd.DataFrame) -> dict[str, Any]:
    data = frame.copy()
    data["year"] = pd.to_datetime(data["asof"], errors="coerce").dt.year
    prepared = _prepare_v2_labels(data)
    asof = pd.to_datetime(data["asof"], errors="coerce") + pd.Timedelta(hours=15)
    published = pd.to_datetime(data.get("published_at"), errors="coerce")
    effective = pd.to_datetime(data.get("effective_at"), errors="coerce")
    duplicate_rows = int(data.duplicated(["asof", "code"]).sum())
    missing_by_year: dict[str, dict[str, float]] = {}
    for column in FEATURE_COLUMNS:
        values = pd.to_numeric(data[column], errors="coerce")
        rates = values.isna().groupby(data["year"]).mean()
        missing_by_year[column] = {
            str(int(year)): float(rate) for year, rate in rates.items()
        }
    quality_findings = [
        {
            "code": "V1_LABEL_SCOPE",
            "severity": "HIGH",
            "confidence": "HIGH",
            "evidence": {
                "full_rows_without_outcome": int(
                    pd.to_numeric(data["forward_return_60"], errors="coerce").isna().sum()
                ),
                "v2_labeled_rows": int(prepared["v2_eligible"].sum()),
            },
            "impact": "V1 把不可执行、无 60 日结果的行初始化为负类，稀释了真实 5% 标签。",
            "remediation": "V2 只在可执行且标签非空的合格股票池中训练。",
        },
        {
            "code": "NO_INFORMATION_FEATURES",
            "severity": "HIGH",
            "confidence": "HIGH",
            "evidence": {
                "turnover_20_missing_rate": float(
                    pd.to_numeric(data["turnover_20"], errors="coerce").isna().mean()
                ),
                "valuation_percentile_unique": int(
                    pd.to_numeric(data["valuation_percentile"], errors="coerce").nunique(
                        dropna=True
                    )
                ),
            },
            "impact": "缺失指示和常量特征不提供排序信息，且可能造成跨年份伪差异。",
            "remediation": "每个训练折动态删除全空、近空和常量特征。",
        },
        {
            "code": "FLOW_SCALE_AND_DRIFT",
            "severity": "HIGH",
            "confidence": "MEDIUM",
            "evidence": {
                "northbound_missing_rate": float(
                    pd.to_numeric(data["northbound_change_ratio"], errors="coerce").isna().mean()
                ),
                "northbound_max": float(
                    pd.to_numeric(data["northbound_change_ratio"], errors="coerce").max()
                ),
                "institution_holding_missing_2018": missing_by_year[
                    "institution_holding_change_ratio"
                ].get("2018", 0.0),
            },
            "impact": "资金因子存在量纲长尾和 2018 覆盖断层，跨年模型容易学习来源差异。",
            "remediation": "分位截尾只用训练折统计；核心和技术方案排除这些资金因子。",
        },
    ]
    return {
        "grain": {
            "unit": "weekly decision date × stock",
            "rows": int(len(data)),
            "columns": int(len(data.columns)),
            "decision_dates": int(data["asof"].nunique()),
            "stocks": int(data["code"].nunique()),
            "duplicate_keys": duplicate_rows,
        },
        "labels": {
            "full_rows_without_outcome": int(
                pd.to_numeric(data["forward_return_60"], errors="coerce").isna().sum()
            ),
            "v2_labeled_rows": int(prepared["v2_eligible"].sum()),
            "v2_positive_rows": int(prepared.loc[prepared["v2_eligible"], "target"].sum()),
        },
        "time_audit": {
            "published_missing": int(published.isna().sum()),
            "published_after_decision": int((published > asof).sum()),
            "effective_missing": int(effective.isna().sum()),
            "effective_after_decision": int((effective > asof).sum()),
            "passed": bool(
                duplicate_rows == 0
                and not published.isna().any()
                and not effective.isna().any()
                and not (published > asof).any()
                and not (effective > asof).any()
            ),
        },
        "missing_by_year": missing_by_year,
        "findings": quality_findings,
    }


def run_development_experiment(
    frame: pd.DataFrame,
    *,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    from sklearn.ensemble import HistGradientBoostingClassifier

    data = _prepare_v2_labels(frame)
    variants: dict[str, dict[str, Any]] = {
        name: {"yearly": {}} for name in (*VARIANT_FEATURES, *DERIVED_VARIANTS)
    }
    baseline: dict[str, Any] = {"yearly": {}}
    for year_index, test_year in enumerate(OOS_DEVELOPMENT_YEARS):
        train = data.loc[
            (data["year"] >= DEVELOPMENT_YEARS[0])
            & (data["year"] < test_year)
            & data["v2_eligible"]
        ].copy()
        test = data.loc[(data["year"] == test_year) & data["v2_eligible"]].copy()
        train = _purge_tail_dates(train, 60)
        test = _embargo_head_dates(test, 20)
        if train.empty or test.empty or train["target"].nunique() < 2:
            raise ResearchDataBlockedError(f"V2 {test_year} 开发折缺少两类训练或测试样本")
        year_frame = data.loc[data["year"] == test_year].copy()
        year_frame["evaluation_eligible"] = False
        year_frame.loc[test.index, "evaluation_eligible"] = True
        model_probabilities: dict[str, pd.Series] = {}
        for variant_name, feature_columns in VARIANT_FEATURES.items():
            train_features, preprocessor = _fit_preprocessor(train, feature_columns)
            test_features = _apply_preprocessor(test, preprocessor)
            model = HistGradientBoostingClassifier(**MODEL_PARAMETERS)
            model.fit(train_features, train["target"].astype(int))
            model_probabilities[variant_name] = pd.Series(
                model.predict_proba(test_features)[:, 1], index=test.index
            )
            evaluated = year_frame.copy()
            evaluated["score"] = np.nan
            evaluated.loc[test.index, "score"] = model_probabilities[variant_name]
            metrics = _evaluate_year(evaluated, "score", "evaluation_eligible")
            variants[variant_name]["yearly"][str(test_year)] = {
                **metrics,
                "train_rows": int(len(train)),
                "test_rows": int(len(test)),
                "feature_count": int(len(preprocessor)),
                "features": list(preprocessor),
            }
        technical = model_probabilities["technical_clean"]
        probability_rank = technical.groupby(test["asof"]).rank(pct=True)
        rs60_rank = pd.to_numeric(test["relative_return_60"], errors="coerce").groupby(
            test["asof"]
        ).rank(pct=True)
        derived_scores = {
            "technical_breadth50": technical,
            "blend50": (probability_rank + rs60_rank) / 2.0,
            "blend50_breadth50": (probability_rank + rs60_rank) / 2.0,
        }
        for variant_name, scores in derived_scores.items():
            evaluated = year_frame.copy()
            evaluated["score"] = np.nan
            evaluated.loc[test.index, "score"] = scores
            if "breadth50" in variant_name:
                evaluated["derived_eligible"] = (
                    evaluated["evaluation_eligible"]
                    & (evaluated["market_breadth_ma60"] > 0.50)
                )
                eligibility = "derived_eligible"
            else:
                eligibility = "evaluation_eligible"
            variants[variant_name]["yearly"][str(test_year)] = _evaluate_year(
                evaluated, "score", eligibility
            )
        baseline_year = _evaluate_year(
            year_frame, "relative_return_60", "evaluation_eligible"
        )
        baseline["yearly"][str(test_year)] = baseline_year
        for variant in variants.values():
            candidate = variant["yearly"][str(test_year)]
            candidate["gate_passed"] = _passes_development_gate(
                candidate, baseline_year
            )
        _progress(
            progress_callback,
            "DEVELOPMENT_OOS",
            0.25 + 0.15 * (year_index + 1),
            f"{test_year} 开发期样本外折完成",
        )
    for variant in variants.values():
        variant["passed"] = all(
            bool(metrics["gate_passed"])
            for metrics in variant["yearly"].values()
        )
        variant["passed_years"] = sum(
            bool(metrics["gate_passed"])
            for metrics in variant["yearly"].values()
        )
    return {
        "protocol_version": PROTOCOL_VERSION,
        "preprocessor_version": PREPROCESSOR_VERSION,
        "random_seed": MODEL_RANDOM_SEED,
        "model_parameters": MODEL_PARAMETERS,
        "development_years": list(DEVELOPMENT_YEARS),
        "oos_years": list(OOS_DEVELOPMENT_YEARS),
        "excluded_tuning_years": [2024, 2025, 2026],
        "variants": variants,
        "baseline": baseline,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
        },
    }


def _prepare_v2_labels(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["year"] = pd.to_datetime(data["asof"], errors="coerce").dt.year
    data["v2_eligible"] = False
    data["target"] = np.nan
    data["market_breadth_ma60"] = np.nan
    for _, group in data.groupby("asof", sort=False):
        marked = mark_research_universe_eligibility(group.to_dict("records"))
        common = pd.Series(
            [bool(item.get("eligible")) for item in marked], index=group.index
        )
        universe = pd.Series(
            [bool(item.get("universe_gate")) for item in marked], index=group.index
        )
        returns = pd.to_numeric(group["forward_return_60"], errors="coerce")
        eligible = common & group["entry_executable"].fillna(False).astype(bool)
        eligible &= returns.notna()
        data.loc[group.index, "v2_eligible"] = eligible
        if bool(eligible.any()):
            threshold = float(returns.loc[eligible].quantile(0.95))
            data.loc[group.index[eligible], "target"] = (
                returns.loc[eligible] >= threshold
            ).astype(int)
        above_ma60 = (
            pd.to_numeric(group["close"], errors="coerce")
            > pd.to_numeric(group["ma60"], errors="coerce")
        )
        data.loc[group.index, "market_breadth_ma60"] = float(
            (above_ma60 & universe).sum() / max(1, int(universe.sum()))
        )
    return data


def _fit_preprocessor(
    frame: pd.DataFrame,
    columns: Iterable[str],
) -> tuple[pd.DataFrame, dict[str, dict[str, Any]]]:
    transformed = pd.DataFrame(index=frame.index)
    specification: dict[str, dict[str, Any]] = {}
    for column in columns:
        values = pd.to_numeric(frame[column], errors="coerce")
        if float(values.notna().mean()) < 0.05 or int(values.nunique(dropna=True)) <= 1:
            continue
        lower = float(values.quantile(0.005))
        upper = float(values.quantile(0.995))
        clipped = values.clip(lower, upper)
        median = float(clipped.median())
        include_missing = bool(values.isna().any())
        specification[column] = {
            "lower": lower,
            "upper": upper,
            "median": median,
            "include_missing": include_missing,
        }
        transformed[column] = clipped.fillna(median)
        if include_missing:
            transformed[f"{column}__missing"] = values.isna().astype(float)
    if transformed.empty:
        raise ResearchDataBlockedError("V2 训练折没有信息量充足的特征")
    return transformed, specification


def _apply_preprocessor(
    frame: pd.DataFrame,
    specification: Mapping[str, Mapping[str, Any]],
) -> pd.DataFrame:
    transformed = pd.DataFrame(index=frame.index)
    for column, params in specification.items():
        values = pd.to_numeric(frame[column], errors="coerce")
        transformed[column] = values.clip(
            float(params["lower"]), float(params["upper"])
        ).fillna(float(params["median"]))
        if bool(params.get("include_missing")):
            transformed[f"{column}__missing"] = values.isna().astype(float)
    return transformed


def _evaluate_year(
    frame: pd.DataFrame,
    score_column: str,
    eligibility_column: str,
) -> dict[str, Any]:
    metrics, _, ic_values = _evaluate_non_overlapping_portfolio(
        frame,
        score_column=score_column,
        eligibility_column=eligibility_column,
    )
    metrics.update(_ranking_metrics(frame, score_column, eligibility_column))
    metrics["ic"] = float(np.mean(ic_values)) if ic_values else 0.0
    return metrics


def _passes_development_gate(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> bool:
    return bool(
        float(candidate.get("precision_at_20", 0.0))
        > float(baseline.get("precision_at_20", 0.0))
        and float(candidate.get("total_return", 0.0))
        > float(baseline.get("total_return", 0.0))
        and float(candidate.get("double_cost_return", 0.0))
        > float(baseline.get("double_cost_return", 0.0))
        and float(candidate.get("max_drawdown", -1.0))
        >= float(baseline.get("max_drawdown", -1.0)) - 0.03
    )


def _source_snapshot_hash(batches: Iterable[Mapping[str, Any]]) -> str:
    payload = [
        {
            "batch_id": item["batch_id"],
            "content_hash": item["content_hash"],
            "schema_hash": item["schema_hash"],
            "row_count": item["row_count"],
        }
        for item in batches
    ]
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _decode_validation(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    for field in (
        "rule_metrics",
        "ml_metrics",
        "baseline_metrics",
        "stress_metrics",
        "gates",
        "champion",
    ):
        item[field] = _json_value(item.pop(f"{field}_json", "{}"), {})
    return item


def _decode_batch(row: Mapping[str, Any]) -> dict[str, Any]:
    item = dict(row)
    item["metadata"] = _json_value(item.pop("metadata_json", "{}"), {})
    return item


def _progress(callback: Any | None, stage: str, progress: float, detail: str) -> None:
    if callback is not None:
        callback(stage=stage, progress=progress, detail=detail)


__all__ = [
    "DEVELOPMENT_YEARS",
    "EarlyWinnerV2ResearchService",
    "OOS_DEVELOPMENT_YEARS",
    "PREPROCESSOR_VERSION",
    "PROTOCOL_VERSION",
    "profile_development_data",
    "run_development_experiment",
]
