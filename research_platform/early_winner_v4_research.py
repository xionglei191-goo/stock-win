from __future__ import annotations

import hashlib
import json
import pickle
import platform
import re
import scipy
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd
import sklearn

from .config import PlatformConfig
from .delisted_history_quality import (
    AUDIT_END as DELISTED_HISTORY_AUDIT_END,
    AUDIT_START as DELISTED_HISTORY_AUDIT_START,
    DATASET_CONTRACTS as DELISTED_HISTORY_DATASET_CONTRACTS,
    DELISTED_HISTORY_QUALITY_REJECTED,
    DELISTED_HISTORY_SOURCE_INCOMPLETE,
    PROTOCOL_VERSION as DELISTED_HISTORY_PROTOCOL_VERSION,
    QUALITY_POLICY_VERSION as DELISTED_HISTORY_QUALITY_POLICY_VERSION,
    READY as DELISTED_HISTORY_READY,
    REQUIRED_DATASETS as DELISTED_HISTORY_REQUIRED_DATASETS,
    load_verified_delisted_history_gate,
)
from .early_winner_research import (
    MODEL_PARAMETERS,
    MODEL_RANDOM_SEED,
    ResearchDataBlockedError,
    TdxResearchHttpClient,
    _embargo_head_dates,
    _portfolio_metrics,
    _purge_tail_dates,
    _select_with_industry_cap,
    _decision_timestamps,
    _field_values,
)
from .early_winner_v2_research import (
    DEVELOPMENT_YEARS,
    OOS_DEVELOPMENT_YEARS,
    TECHNICAL_FEATURES,
    _apply_preprocessor,
    _decode_batch,
    _decode_validation,
    _fit_preprocessor,
    _json_value,
    _progress,
)
from .early_winner_v3_research import FEATURE_DATASET as V3_FEATURE_DATASET
from .early_winner_v3_research import _hash_payload
from .historical_security_master import load_historical_universe_master_gate
from .storage import Database, _file_sha256
from .strategies.early_winner import (
    attach_execution_outcomes,
    mark_research_universe_eligibility,
)
from .strategies.early_winner_v4 import EarlyWinnerV4Strategy, PROJECT_ID


PROJECT_VERSION = "4.0.0-dev2"
PROJECT_NAME = "早期强势股识别 V4"
PROJECT_DESCRIPTION = (
    "冻结 2018—2023 开发区，使用未复权下一开盘到第40个交易日下一开盘收益，"
    "只在合格股票池 MA60 广度大于50%时训练和评价。2024/2025 与 2026 保持封存。"
)
FEATURE_DATASET = "early_winner_v4_features_v1"
RAW_DATASET = "tdx_raw_ohlcv_label_history"
EXECUTION_STATUS_DATASET = "tdx_execution_status_history"
RAW_FIELDS = ("Open", "High", "Low", "Close", "Volume", "Amount", "ForwardFactor")
EXECUTION_STATUS_FIELDS = ("GP15", "GP29", "GP30", "GP43")
RAW_SOURCE_PROTOCOL_VERSION = "early-winner-v4-raw-through-2023-v1"
EXECUTION_STATUS_PROTOCOL_VERSION = "early-winner-v4-execution-status-v3"
EXECUTION_STATUS_INDEX_PROTOCOL_VERSION = "early-winner-v4-status-index-v2"
BUILD_PROTOCOL_VERSION = "early-winner-v4-40d-label-v10"
DEVELOPMENT_PROTOCOL_VERSION = "early-winner-v4-development-v7"
PREPROCESSOR_VERSION = "early-winner-v4-train-only-winsor-v1"
HOLDING_TRADING_DAYS = 40
EMBARGO_TRADING_DAYS = 20
MARKET_BREADTH_THRESHOLD = 0.50
TARGET_QUANTILE = 0.90
TARGET_REQUIRES_POSITIVE_RETURN = True
RETURN_COLUMN = "forward_return_40"
PORTFOLIO_SIZE = 20
NON_OVERLAP_PHASES = 8
MINIMUM_PHASE_PERIODS = 3
MINIMUM_PHASE_INVESTED_PERIODS = 2
RAW_START = "20171201"
RAW_END = "20231231"
DELISTED_HISTORY_ARTIFACT_INVALID = "DELISTED_HISTORY_ARTIFACT_INVALID"


class EarlyWinnerV4ResearchService:
    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.strategy = EarlyWinnerV4Strategy()
        current = self.database.query(
            "SELECT status, data_asof, data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        status = str(current[0]["status"]) if current else "DATA_BUILDING"
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
        rows = self.database.query(
            "SELECT * FROM research_projects WHERE project_id=?", (PROJECT_ID,)
        )
        if not rows:
            raise KeyError(PROJECT_ID)
        project = dict(rows[0])
        project["data_gates"] = _json_value(project.pop("data_gates_json", "{}"), {})
        historical_master_gate = self._historical_universe_gate()
        project["data_gates"]["historical_universe_master"] = historical_master_gate
        delisted_history_gate = self._delisted_history_gate(
            historical_master_gate=historical_master_gate
        )
        project["data_gates"]["delisted_history_quality"] = delisted_history_gate
        if not historical_master_gate.get("ready") or not delisted_history_gate.get(
            "ready"
        ):
            project["status"] = "BLOCKED_DATA"
        validations = self.database.query(
            "SELECT * FROM research_validations WHERE project_id=? ORDER BY created_at DESC LIMIT 1",
            (PROJECT_ID,),
        )
        project["latest_development_audit"] = (
            _decode_validation(validations[0]) if validations else None
        )
        batches = self.database.query(
            "SELECT * FROM research_data_batches WHERE project_id=? ORDER BY fetched_at DESC LIMIT 20",
            (PROJECT_ID,),
        )
        decoded_batches = [_decode_batch(row) for row in batches]
        project["latest_batches"] = decoded_batches
        project["strategy"] = {
            "strategy_id": self.strategy.metadata.strategy_id,
            "version": self.strategy.metadata.version,
            "name": self.strategy.metadata.name,
            "lifecycle": self.strategy.metadata.lifecycle,
            "category": self.strategy.metadata.category.value,
            "scan_enabled": self.strategy.metadata.scan_enabled,
            "backtest_enabled": self.strategy.metadata.backtest_enabled,
        }
        project["protocol"] = {
            "holding_trading_days": HOLDING_TRADING_DAYS,
            "embargo_trading_days": EMBARGO_TRADING_DAYS,
            "market_breadth_threshold": MARKET_BREADTH_THRESHOLD,
            "target_quantile": TARGET_QUANTILE,
            "target_requires_positive_return": TARGET_REQUIRES_POSITIVE_RETURN,
            "feature_set": list(TECHNICAL_FEATURES),
            "random_seed": MODEL_RANDOM_SEED,
        }
        project["development_years"] = list(DEVELOPMENT_YEARS)
        project["excluded_tuning_years"] = [2024, 2025, 2026]
        project["frozen_validation_opened"] = False
        project["candidate_generation_enabled"] = False
        project["trade_signals_enabled"] = False
        project["promotion_allowed"] = False
        label_gate = dict(project["data_gates"].get("label_snapshot") or {})
        if label_gate.get("ready"):
            active_snapshot = str(label_gate.get("snapshot_hash") or "")
            matching = 0
            for row in decoded_batches:
                metadata = dict(row.get("metadata") or {})
                if (
                    row.get("dataset") == FEATURE_DATASET
                    and metadata.get("protocol_version") == BUILD_PROTOCOL_VERSION
                    and metadata.get("snapshot_hash") == active_snapshot
                    and metadata.get("delisted_history_manifest_hash")
                    == delisted_history_gate.get("manifest_hash")
                    and metadata.get("delisted_history_report_hash")
                    == delisted_history_gate.get("report_hash")
                ):
                    matching += 1
            if matching != len(DEVELOPMENT_YEARS):
                project["status"] = "BLOCKED_DATA"
                project["data_gates"]["label_snapshot"] = {
                    **label_gate,
                    "ready": False,
                    "status": "REBUILD_REQUIRED",
                    "detail": "Active label snapshot does not match the current protocol",
                }
        return project

    def build_label_snapshot(
        self, progress_callback: Any | None = None
    ) -> dict[str, Any]:
        historical_master_gate = self._require_historical_universe_master()
        delisted_history_gate = self._require_delisted_history_quality(
            historical_master_gate=historical_master_gate
        )
        self.database.update_research_project(PROJECT_ID, status="DATA_BUILDING")
        _progress(progress_callback, "TDX_ADMISSION", 0.02, "检查 TDX 未复权日线准入")
        client = TdxResearchHttpClient(tdx_root=self.config.tdx_root)
        admission = client.admission_probe()
        if not admission.ready:
            raise ResearchDataBlockedError(
                f"TDX 字段准入失败: {admission.status}: {admission.detail}"
            )
        source_batches = self._v3_batches(
            historical_master_snapshot=str(historical_master_gate["snapshot_id"])
        )
        calendar_batch, trading_calendar = self._trading_calendar()
        frames = {
            int(str(item["published_end"])[:4]): pd.read_parquet(str(item["path"]))
            for item in source_batches
        }
        codes = sorted({str(code) for frame in frames.values() for code in frame["code"].unique()})
        _progress(progress_callback, "RAW_LABEL_BARS", 0.05, f"读取 {len(codes)} 只股票未复权标签日线")
        raw_files = self._build_raw_bar_files(client, codes, progress_callback=progress_callback)
        status_files = self._build_execution_status_files(
            client, codes, progress_callback=progress_callback
        )
        raw_hash = _hash_payload(
            [(code, _file_sha256(path)) for code, path in sorted(raw_files.items())]
        )
        status_hash = _hash_payload(
            [(code, _file_sha256(path)) for code, path in sorted(status_files.items())]
        )
        raw_index = self._persist_raw_index(raw_files, raw_hash)
        status_profile = self._execution_status_profile(status_files)
        status_index = self._persist_execution_status_index(status_files, status_hash)
        incomplete_status_codes = set(status_profile["all_empty_codes"])
        snapshot_hash = _hash_payload(
            {
                "protocol": BUILD_PROTOCOL_VERSION,
                "v3_features": [
                    (item["batch_id"], item["content_hash"]) for item in source_batches
                ],
                "raw_hash": raw_hash,
                "raw_range": [RAW_START, RAW_END],
                "raw_protocol": RAW_SOURCE_PROTOCOL_VERSION,
                "raw_fields": list(RAW_FIELDS),
                "execution_status_hash": status_hash,
                "execution_status_protocol": EXECUTION_STATUS_PROTOCOL_VERSION,
                "execution_status_fields": list(EXECUTION_STATUS_FIELDS),
                "holding_days": HOLDING_TRADING_DAYS,
                "calendar_batch": calendar_batch["batch_id"],
                "calendar_hash": calendar_batch["content_hash"],
                "historical_security_master_snapshot": historical_master_gate[
                    "snapshot_id"
                ],
                "historical_security_master_manifest": historical_master_gate[
                    "manifest_hash"
                ],
                "delisted_history_protocol": DELISTED_HISTORY_PROTOCOL_VERSION,
                "delisted_history_quality_policy": (
                    DELISTED_HISTORY_QUALITY_POLICY_VERSION
                ),
                "delisted_history_manifest_hash": delisted_history_gate[
                    "manifest_hash"
                ],
                "delisted_history_report_hash": delisted_history_gate["report_hash"],
            }
        )
        records: list[dict[str, Any]] = []
        profiles: dict[str, Any] = {}
        for index, year in enumerate(DEVELOPMENT_YEARS):
            existing = self._existing_year(year, snapshot_hash=snapshot_hash)
            if existing is not None:
                records.append(existing)
                profiles[str(year)] = dict(existing["metadata"]["profile"])
                continue
            _progress(
                progress_callback,
                "DERIVE_40D_LABELS",
                0.72 + 0.04 * index,
                f"构建 {year} 年 40 日执行标签",
            )
            frame = frames[year]
            frame = frame.copy()
            frame["execution_status_complete"] = ~frame["code"].astype(str).isin(
                incomplete_status_codes
            )
            if "eligible" in frame:
                frame["eligible"] = (
                    frame["eligible"].fillna(False).astype(bool)
                    & frame["execution_status_complete"]
                )
            if "universe_gate" in frame:
                frame["universe_gate"] = (
                    frame["universe_gate"].fillna(False).astype(bool)
                    & frame["execution_status_complete"]
                )
            bars = {
                code: self._read_raw_bars(raw_files[code])
                for code in frame["code"].astype(str).unique()
            }
            security_status: dict[str, pd.Series] = {}
            limit_status: dict[str, pd.Series] = {}
            for code in frame["code"].astype(str).unique():
                statuses = self._read_execution_status(status_files[code])
                security_status[code] = statuses.get(
                    "GP29", pd.Series(dtype=float)
                )
                limit_status[code] = statuses.get(
                    "GP15", pd.Series(dtype=float)
                )
            derived = attach_execution_outcomes(
                frame,
                bars,
                holding_days=HOLDING_TRADING_DAYS,
                trading_calendar=trading_calendar,
                require_forward_factor=True,
                security_status_history=security_status,
                limit_status_history=limit_status,
            )
            development_cutoff = pd.Timestamp("2023-12-31")
            planned_exit = pd.to_datetime(derived["planned_exit_time"], errors="coerce")
            actual_exit = pd.to_datetime(derived["exit_time"], errors="coerce")
            derived["label_window_matured"] = planned_exit.notna() & (
                planned_exit <= development_cutoff
            )
            derived["label_matured_in_development"] = (
                derived["label_window_matured"]
                & actual_exit.notna()
                & (actual_exit <= development_cutoff)
            )
            # No execution outcome that crosses the frozen 2023 boundary may
            # survive, including a nominal December exit delayed into 2024.
            immature = actual_exit.isna() | (actual_exit > development_cutoff)
            derived.loc[
                immature,
                [
                    "entry_executable",
                    "entry_time",
                    "entry_price",
                    "entry_forward_factor",
                    "exit_time",
                    "exit_price",
                    "exit_forward_factor",
                    "exit_delay_days",
                    "exit_reason",
                    "order_value",
                    RETURN_COLUMN,
                ],
            ] = [
                False,
                None,
                np.nan,
                np.nan,
                None,
                np.nan,
                np.nan,
                0,
                None,
                0.0,
                np.nan,
            ]
            profile = profile_v4_data(derived, year=year)
            if profile["label_coverage"] < 0.95:
                raise ResearchDataBlockedError(
                    f"V4 {year} 40日标签覆盖率不足: {profile['label_coverage']:.4f}"
                )
            if profile["duplicate_grain_rows"] or not profile["timing_audit_passed"]:
                raise ResearchDataBlockedError(f"V4 {year} 粒度或时点审计失败")
            record = self._persist_year(
                year,
                derived,
                snapshot_hash=snapshot_hash,
                profile=profile,
                historical_master_snapshot=str(historical_master_gate["snapshot_id"]),
                delisted_history_manifest_hash=str(
                    delisted_history_gate["manifest_hash"]
                ),
                delisted_history_report_hash=str(delisted_history_gate["report_hash"]),
            )
            records.append(record)
            profiles[str(year)] = profile
        manifest = {
            "project_id": PROJECT_ID,
            "protocol_version": BUILD_PROTOCOL_VERSION,
            "snapshot_hash": snapshot_hash,
            "source_batches": [item["batch_id"] for item in source_batches],
            "raw_source_index": raw_index["batch_id"],
            "raw_hash": raw_hash,
            "execution_status_index": status_index["batch_id"],
            "execution_status_hash": status_hash,
            "execution_status_profile": status_profile,
            "calendar_batch": calendar_batch["batch_id"],
            "calendar_hash": calendar_batch["content_hash"],
            "historical_security_master_snapshot": historical_master_gate[
                "snapshot_id"
            ],
            "historical_security_master_manifest": historical_master_gate[
                "manifest_hash"
            ],
            "delisted_history_protocol": DELISTED_HISTORY_PROTOCOL_VERSION,
            "delisted_history_quality_policy": DELISTED_HISTORY_QUALITY_POLICY_VERSION,
            "delisted_history_manifest_hash": delisted_history_gate["manifest_hash"],
            "delisted_history_report_hash": delisted_history_gate["report_hash"],
            "label_outcome_cutoff": "2023-12-31; both planned and actual exit must mature",
            "profiles": profiles,
            "shards": [
                {
                    "batch_id": item["batch_id"],
                    "path": item["path"],
                    "content_hash": item["content_hash"],
                    "row_count": item["row_count"],
                }
                for item in records
            ],
        }
        manifest_path = self._persist_manifest(snapshot_hash, manifest)
        gates = {
            "historical_universe_master": historical_master_gate,
            "delisted_history_quality": delisted_history_gate,
            "tdx_raw_ohlcv": {
                "ready": True,
                "detail": "TDX dividend_type=none；下一交易日开盘入场，第40个交易日后下一开盘退出",
            },
            "tdx_execution_status": {
                "ready": True,
                "detail": "GP15/GP29/GP30/GP43 frozen; incomplete codes excluded",
                "profile": status_profile,
            },
            "label_snapshot": {
                "ready": True,
                "snapshot_hash": snapshot_hash,
                "manifest": str(manifest_path),
                "years": list(DEVELOPMENT_YEARS),
                "delisted_history_manifest_hash": delisted_history_gate[
                    "manifest_hash"
                ],
                "delisted_history_report_hash": delisted_history_gate["report_hash"],
            },
            "point_in_time_features": {
                "ready": True,
                "detail": "继承 V3 内容寻址点时特征，仅添加未来执行标签",
            },
            "trading_calendar": {
                "ready": True,
                "detail": "冻结沪深交易日历，并用于20日隔离期与真实退出时点清洗",
                "content_hash": calendar_batch["content_hash"],
            },
            "development_audit": {"ready": False, "status": "REQUIRED"},
            "frozen_2024_2025": {"ready": False, "status": "SEALED"},
        }
        self.database.update_research_project(
            PROJECT_ID,
            status="DEVELOPMENT_AUDIT_REQUIRED",
            data_asof="2023-12-31",
            data_gates=gates,
        )
        _progress(progress_callback, "COMPLETED", 1.0, "V4 40日标签快照完成")
        return {"project_id": PROJECT_ID, "status": "DEVELOPMENT_AUDIT_REQUIRED", **manifest}

    def _existing_year(
        self, year: int, *, snapshot_hash: str
    ) -> dict[str, Any] | None:
        batch_id = f"ewv4f_{snapshot_hash[:20]}_{year}"
        rows = self.database.query(
            "SELECT * FROM research_data_batches WHERE batch_id=? AND status='SUCCEEDED'",
            (batch_id,),
        )
        if not rows:
            return None
        record = dict(rows[0])
        path = Path(str(record["path"]))
        if not path.exists() or _file_sha256(path) != record["content_hash"]:
            raise ResearchDataBlockedError(f"V4 已冻结分片哈希失败: {batch_id}")
        metadata = _json_value(record.pop("metadata_json", "{}"), {})
        if metadata.get("snapshot_hash") != snapshot_hash:
            raise ResearchDataBlockedError(f"V4 已冻结分片快照标识冲突: {batch_id}")
        record["metadata"] = metadata
        return record

    def run_development_audit(
        self, progress_callback: Any | None = None
    ) -> dict[str, Any]:
        historical_master_gate = self._require_historical_universe_master()
        delisted_history_gate = self._require_delisted_history_quality(
            historical_master_gate=historical_master_gate
        )
        batches = self._current_v4_batches(
            historical_master_snapshot=str(historical_master_gate["snapshot_id"]),
            delisted_history_manifest_hash=str(
                delisted_history_gate["manifest_hash"]
            ),
            delisted_history_report_hash=str(delisted_history_gate["report_hash"]),
        )
        self.database.update_research_project(PROJECT_ID, status="DEVELOPMENT_AUDITING")
        frame = pd.concat(
            [pd.read_parquet(str(item["path"])) for item in batches], ignore_index=True
        ).sort_values(["asof", "code"]).reset_index(drop=True)
        profile = profile_v4_data(frame)
        if (
            profile["label_coverage"] < 0.95
            or profile["duplicate_grain_rows"]
            or not profile["timing_audit_passed"]
        ):
            raise ResearchDataBlockedError("V4 开发特征质量门禁失败")
        source_hash = _source_snapshot_hash(batches)
        calendar_batch, trading_calendar = self._trading_calendar()
        model_directory = self.config.runtime_dir / "research" / PROJECT_ID / "models"
        experiment = run_v4_development_experiment(
            frame,
            model_directory=model_directory,
            source_hash=source_hash,
            trading_calendar=trading_calendar,
            progress_callback=progress_callback,
        )
        passing = bool(experiment["model"]["passed"])
        status = "DEVELOPMENT_READY" if passing else "DEVELOPMENT_REJECTED"
        protocol_hash = _hash_payload(
            {
                "protocol": DEVELOPMENT_PROTOCOL_VERSION,
                "preprocessor": PREPROCESSOR_VERSION,
                "source_hash": source_hash,
                "parameters": MODEL_PARAMETERS,
                "features": list(TECHNICAL_FEATURES),
                "holding_days": HOLDING_TRADING_DAYS,
                "embargo_days": EMBARGO_TRADING_DAYS,
                "breadth": MARKET_BREADTH_THRESHOLD,
                "target_quantile": TARGET_QUANTILE,
                "positive_only": TARGET_REQUIRES_POSITIVE_RETURN,
                "portfolio_size": PORTFOLIO_SIZE,
                "non_overlap_phases": NON_OVERLAP_PHASES,
                "minimum_phase_periods": MINIMUM_PHASE_PERIODS,
                "minimum_phase_invested_periods": MINIMUM_PHASE_INVESTED_PERIODS,
                "unfilled_slot_policy": "CASH_NO_REFILL",
                "cycle_turnover_policy": "FULL_EXIT_REBUILD_ON_FILLED_NOTIONAL",
                "paired_cycle_policy": "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
                "calendar_hash": calendar_batch["content_hash"],
                "historical_security_master_snapshot": historical_master_gate[
                    "snapshot_id"
                ],
                "historical_security_master_manifest": historical_master_gate[
                    "manifest_hash"
                ],
                "delisted_history_protocol": DELISTED_HISTORY_PROTOCOL_VERSION,
                "delisted_history_quality_policy": (
                    DELISTED_HISTORY_QUALITY_POLICY_VERSION
                ),
                "delisted_history_manifest_hash": delisted_history_gate[
                    "manifest_hash"
                ],
                "delisted_history_report_hash": delisted_history_gate["report_hash"],
            }
        )
        audit_id = f"ewv4_dev_{protocol_hash[:24]}"
        previous = self.database.query(
            "SELECT created_at FROM research_validations WHERE validation_id=?", (audit_id,)
        )
        now = str(previous[0]["created_at"]) if previous else datetime.now().astimezone().isoformat()
        feature_hash = _hash_payload(list(TECHNICAL_FEATURES))
        for test_year, metrics in experiment["model"]["yearly"].items():
            self.database.save_research_model(
                {
                    "model_id": f"ewv4_{test_year}_{str(metrics['model_hash'])[:24]}",
                    "project_id": PROJECT_ID,
                    "strategy_id": self.strategy.metadata.strategy_id,
                    "status": "SUCCEEDED",
                    "created_at": now,
                    "artifact_path": str(metrics.get("model_path") or ""),
                    "artifact_hash": str(metrics["model_hash"]),
                    "feature_schema_hash": feature_hash,
                    "training_start": "2018-01-01",
                    "training_end": f"{int(test_year) - 1}-12-31",
                    "validation_start": None,
                    "validation_end": None,
                    "test_start": f"{test_year}-01-01",
                    "test_end": f"{test_year}-12-31",
                    "random_seed": MODEL_RANDOM_SEED,
                    "library_version": sklearn.__version__,
                    "metrics": dict(metrics),
                    "metadata": {
                        "protocol_version": DEVELOPMENT_PROTOCOL_VERSION,
                        "source_snapshot_hash": source_hash,
                        "feature_hash": feature_hash,
                        "features": list(TECHNICAL_FEATURES),
                        "dependencies": experiment["environment"],
                        "delisted_history_manifest_hash": delisted_history_gate[
                            "manifest_hash"
                        ],
                        "delisted_history_report_hash": delisted_history_gate[
                            "report_hash"
                        ],
                    },
                    "error": "",
                }
            )
        gates = {
            "market_conditioned_ml": {
                "yearly": {
                    year: bool(metrics["gate_passed"])
                    for year, metrics in experiment["model"]["yearly"].items()
                },
                "passed": passing,
            }
        }
        payload = {
            "audit_id": audit_id,
            "project_id": PROJECT_ID,
            "status": status,
            "created_at": now,
            "protocol_version": DEVELOPMENT_PROTOCOL_VERSION,
            "protocol_hash": protocol_hash,
            "source_snapshot_hash": source_hash,
            "profile": profile,
            "experiment": experiment,
            "passing_variants": ["market_conditioned_ml"] if passing else [],
            "frozen_validation_opened": False,
            "tuning_exclusions": [2024, 2025, 2026],
            "promotion_allowed": False,
            "delisted_history_quality": delisted_history_gate,
        }
        audit_path = self._persist_audit(audit_id, payload)
        self.database.save_research_data_batch(
            {
                "batch_id": audit_id,
                "project_id": PROJECT_ID,
                "dataset": "early_winner_v4_development_audit",
                "source": "early_winner_v4_frozen_2018_2023",
                "status": "SUCCEEDED",
                "fetched_at": now,
                "published_start": "2018-01-01",
                "published_end": "2023-12-31",
                "row_count": len(frame),
                "path": str(audit_path),
                "content_hash": _file_sha256(audit_path),
                "schema_hash": protocol_hash,
                "metadata": {
                    "passing_variants": payload["passing_variants"],
                    "frozen_validation_opened": False,
                },
                "error": "",
            }
        )
        self.database.save_research_validation(
            {
                "validation_id": audit_id,
                "project_id": PROJECT_ID,
                "status": status,
                "created_at": now,
                "finished_at": now,
                "snapshot_id": f"ewv4fs_{source_hash[:32]}",
                "rule_metrics": {},
                "ml_metrics": experiment["model"],
                "baseline_metrics": experiment["baseline"],
                "stress_metrics": {"profile": profile, "protocol": experiment["protocol"]},
                "gates": gates,
                "champion": {},
                "error": "" if passing else "V4 did not pass every 2020-2023 OOS year",
            }
        )
        project_gates = {
            "historical_universe_master": historical_master_gate,
            "delisted_history_quality": delisted_history_gate,
            "label_snapshot": {
                "ready": True,
                "label_coverage": profile["label_coverage"],
                "return_column": RETURN_COLUMN,
                "calendar_hash": calendar_batch["content_hash"],
                "delisted_history_manifest_hash": delisted_history_gate[
                    "manifest_hash"
                ],
                "delisted_history_report_hash": delisted_history_gate["report_hash"],
            },
            "development_stability": {
                "ready": passing,
                "detail": "2020—2023 逐年通过" if passing else "至少一个开发期样本外年份未通过",
            },
            "frozen_2024_2025": {
                "ready": False,
                "status": "VALIDATION_REQUIRED" if passing else "SEALED",
            },
        }
        self.database.update_research_project(
            PROJECT_ID,
            status=status,
            data_asof="2023-12-31",
            data_gates=project_gates,
        )
        _progress(progress_callback, "COMPLETED", 1.0, f"V4 开发审计完成：{status}")
        return {
            "project_id": PROJECT_ID,
            **payload,
            "project_status": status,
            "historical_universe_master": project_gates[
                "historical_universe_master"
            ],
        }

    def _historical_universe_gate(self) -> dict[str, Any]:
        return load_historical_universe_master_gate(self.config.runtime_dir)

    def _delisted_history_gate(
        self, *, historical_master_gate: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Load and independently verify the frozen delisted-history audit.

        This is intentionally read-only.  Data collection and audit publication are
        separate workflows; opening V4 detail must never fetch or synthesize evidence.
        """

        master_gate = dict(
            historical_master_gate
            if historical_master_gate is not None
            else self._historical_universe_gate()
        )
        return load_verified_delisted_history_gate(
            output_root=(
                self.config.runtime_dir
                / "research"
                / PROJECT_ID
                / "delisted_history_quality"
            ),
            input_cas_root=(
                self.config.runtime_dir
                / "research"
                / PROJECT_ID
                / "delisted_history_inputs"
            ),
            security_master_root=self.config.runtime_dir / "security_master",
            expected_master_gate=master_gate,
        )

        master_gate = dict(
            historical_master_gate
            if historical_master_gate is not None
            else self._historical_universe_gate()
        )
        root = (
            self.config.runtime_dir
            / "research"
            / PROJECT_ID
            / "delisted_history_quality"
        )
        missing = {
            "ready": False,
            "status": DELISTED_HISTORY_SOURCE_INCOMPLETE,
            "detail": (
                "No verified 2018-2023 SSE/SZSE delisted-history quality audit is "
                "available for the current security-master snapshot"
            ),
            "promotion_blocked": True,
            "protocol_version": DELISTED_HISTORY_PROTOCOL_VERSION,
            "quality_policy_version": DELISTED_HISTORY_QUALITY_POLICY_VERSION,
            "audit_window": {
                "start": DELISTED_HISTORY_AUDIT_START,
                "end": DELISTED_HISTORY_AUDIT_END,
            },
            "required_datasets": list(DELISTED_HISTORY_REQUIRED_DATASETS),
            "historical_security_master_snapshot": str(
                master_gate.get("snapshot_id") or ""
            ),
            "manifest_hash": "",
            "report_hash": "",
        }
        pointer_path = root / "current.json"
        if not pointer_path.is_file():
            return missing
        try:
            pointer, _ = self._read_canonical_quality_json(
                pointer_path, "delisted-history current pointer"
            )
            if set(pointer) != {
                "protocol_version",
                "manifest_hash",
                "manifest_path",
            }:
                raise ValueError("current pointer schema drift")
            if pointer.get("protocol_version") != DELISTED_HISTORY_PROTOCOL_VERSION:
                raise ValueError("current pointer protocol mismatch")
            manifest_hash = str(pointer.get("manifest_hash") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", manifest_hash):
                raise ValueError("manifest hash is not SHA-256")
            manifest_path = Path(str(pointer.get("manifest_path") or ""))
            expected_manifest_path = root / "manifests" / f"{manifest_hash}.json"
            if manifest_path.resolve() != expected_manifest_path.resolve():
                raise ValueError("manifest path escapes the frozen audit root")
            manifest, manifest_bytes = self._read_canonical_quality_json(
                manifest_path, "delisted-history manifest"
            )
            if hashlib.sha256(manifest_bytes).hexdigest() != manifest_hash:
                raise ValueError("manifest content hash mismatch")
            if manifest.get("protocol_version") != DELISTED_HISTORY_PROTOCOL_VERSION:
                raise ValueError("manifest protocol mismatch")
            if (
                manifest.get("quality_policy_version")
                != DELISTED_HISTORY_QUALITY_POLICY_VERSION
            ):
                raise ValueError("manifest quality policy mismatch")
            manifest_master = dict(manifest.get("master_identity") or {})
            if (
                manifest_master.get("snapshot_id")
                != master_gate.get("snapshot_id")
                or manifest_master.get("manifest_hash")
                != master_gate.get("manifest_hash")
            ):
                raise ValueError(
                    "audit is not bound to the current historical security master"
                )
            artifacts = dict(manifest.get("artifacts") or {})
            if set(artifacts) != {"audit_report"}:
                raise ValueError("manifest audit artifact schema drift")
            report_identity = dict(artifacts["audit_report"])
            report_hash = str(report_identity.get("content_hash") or "")
            if not re.fullmatch(r"[0-9a-f]{64}", report_hash):
                raise ValueError("report hash is not SHA-256")
            if report_identity.get("cas_uri") != f"sha256:{report_hash}":
                raise ValueError("report CAS URI mismatch")
            report_path = root / "objects" / "sha256" / report_hash[:2] / report_hash
            report, report_bytes = self._read_canonical_quality_json(
                report_path, "delisted-history audit report"
            )
            if hashlib.sha256(report_bytes).hexdigest() != report_hash:
                raise ValueError("report content hash mismatch")
            if int(report_identity.get("byte_count", -1)) != len(report_bytes):
                raise ValueError("report byte_count mismatch")
            if report.get("protocol_version") != DELISTED_HISTORY_PROTOCOL_VERSION:
                raise ValueError("report protocol mismatch")
            if (
                report.get("quality_policy_version")
                != DELISTED_HISTORY_QUALITY_POLICY_VERSION
            ):
                raise ValueError("report quality policy mismatch")
            if dict(report.get("audit_window") or {}) != {
                "start": DELISTED_HISTORY_AUDIT_START,
                "end": DELISTED_HISTORY_AUDIT_END,
            }:
                raise ValueError("report audit window mismatch")
            if dict(report.get("master_identity") or {}) != manifest_master:
                raise ValueError("report and manifest master identities disagree")
            report_gate = dict(report.get("gate") or {})
            common = {
                **missing,
                "status": str(report_gate.get("status") or ""),
                "detail": str(report_gate.get("detail") or ""),
                "manifest_hash": manifest_hash,
                "report_hash": report_hash,
                "hard_failure_count": int(
                    report_gate.get("hard_failure_count", -1)
                ),
                "historical_security_master_snapshot": str(
                    manifest_master.get("snapshot_id") or ""
                ),
            }
            if report_gate.get("ready") is not True:
                if common["status"] not in {
                    DELISTED_HISTORY_SOURCE_INCOMPLETE,
                    DELISTED_HISTORY_QUALITY_REJECTED,
                }:
                    raise ValueError("failed audit has an unsupported status")
                if report_gate.get("promotion_blocked") is not True:
                    raise ValueError("failed audit does not block promotion")
                return common
            self._validate_ready_delisted_history_report(
                report=report,
                manifest=manifest,
            )
            target_scope = dict(report["target_scope"])
            coverage = dict(report["coverage"])
            return {
                **common,
                "ready": True,
                "status": DELISTED_HISTORY_READY,
                "detail": str(report_gate["detail"]),
                "promotion_blocked": False,
                "target_security_count": int(target_scope["security_count"]),
                "coverage_partition_count": len(
                    coverage["by_dataset_exchange_year_code"]
                ),
                "source_dataset_count": len(report["source_hashes"]),
            }
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            return {
                **missing,
                "status": DELISTED_HISTORY_ARTIFACT_INVALID,
                "detail": f"Delisted-history audit artifact validation failed: {exc}",
            }

    @staticmethod
    def _read_canonical_quality_json(
        path: Path, label: str
    ) -> tuple[dict[str, Any], bytes]:
        current = path
        while True:
            if current.exists() and current.is_symlink():
                raise ValueError(f"{label} uses a symlink")
            parent = current.parent
            if parent == current:
                break
            current = parent
        if not path.is_file():
            raise ValueError(f"{label} is missing")
        content = path.read_bytes()
        try:
            value = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{label} is not UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ValueError(f"{label} is not a JSON object")
        canonical = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if canonical != content:
            raise ValueError(f"{label} is not canonical JSON")
        return value, content

    @staticmethod
    def _validate_ready_delisted_history_report(
        *, report: Mapping[str, Any], manifest: Mapping[str, Any]
    ) -> None:
        gate = dict(report.get("gate") or {})
        if (
            gate.get("ready") is not True
            or gate.get("status") != DELISTED_HISTORY_READY
            or gate.get("promotion_blocked") is not False
            or gate.get("caller_ready_and_complete_flags_ignored") is not True
            or int(gate.get("hard_failure_count", -1)) != 0
            or dict(gate.get("finding_counts") or {})
        ):
            raise ValueError("ready report gate is internally inconsistent")
        if list(report.get("findings") or []) or int(
            report.get("findings_truncated", -1)
        ) != 0:
            raise ValueError("ready report still contains findings")
        source_indexes = dict(manifest.get("source_indexes") or {})
        if set(source_indexes) != set(DELISTED_HISTORY_REQUIRED_DATASETS):
            raise ValueError("ready report source-index coverage is incomplete")
        source_rows = list(report.get("source_hashes") or [])
        if len(source_rows) != len(DELISTED_HISTORY_REQUIRED_DATASETS):
            raise ValueError("ready report source-hash summary is incomplete")
        source_by_dataset: dict[str, Mapping[str, Any]] = {}
        for row in source_rows:
            if not isinstance(row, Mapping):
                raise ValueError("source-hash summary row is invalid")
            dataset = str(row.get("dataset") or "")
            if dataset in source_by_dataset:
                raise ValueError("source-hash summary contains a duplicate dataset")
            source_by_dataset[dataset] = row
        if set(source_by_dataset) != set(DELISTED_HISTORY_REQUIRED_DATASETS):
            raise ValueError("ready report source-hash datasets are incomplete")
        for dataset in DELISTED_HISTORY_REQUIRED_DATASETS:
            identity = dict(source_indexes[dataset])
            row = source_by_dataset[dataset]
            index_hash = str(identity.get("content_hash") or "")
            if (
                not re.fullmatch(r"[0-9a-f]{64}", index_hash)
                or identity.get("cas_uri") != f"sha256:{index_hash}"
                or row.get("index_hash") != index_hash
            ):
                raise ValueError(f"{dataset} source index identity mismatch")
            contract = DELISTED_HISTORY_DATASET_CONTRACTS[dataset]
            if (
                row.get("source_protocol_version")
                != contract.source_protocol_version
                or row.get("schema_version") != contract.schema_version
                or not str(row.get("source_authority") or "").strip()
                or int(row.get("row_count", -1)) < 0
            ):
                raise ValueError(f"{dataset} source contract mismatch")
            raw_hashes = list(row.get("raw_source_hashes") or [])
            if not raw_hashes or any(
                not re.fullmatch(r"[0-9a-f]{64}", str(value))
                for value in raw_hashes
            ):
                raise ValueError(f"{dataset} raw source hashes are incomplete")
        target_scope = dict(report.get("target_scope") or {})
        security_count = int(target_scope.get("security_count", 0))
        codes = list(target_scope.get("codes") or [])
        exchange_counts = dict(target_scope.get("exchange_counts") or {})
        if (
            security_count <= 0
            or len(codes) != security_count
            or len(set(codes)) != security_count
            or int(exchange_counts.get("SSE", 0))
            + int(exchange_counts.get("SZSE", 0))
            != security_count
            or any(not re.fullmatch(r"\d{6}\.(?:SH|SZ)", str(code)) for code in codes)
        ):
            raise ValueError("ready report target scope is inconsistent")
        coverage = dict(report.get("coverage") or {})
        if list(coverage.get("missing_sample") or []):
            raise ValueError("ready report has missing coverage samples")
        code_rows = list(coverage.get("by_dataset_exchange_year_code") or [])
        aggregate_rows = list(coverage.get("by_dataset_exchange_year") or [])
        if not code_rows or not aggregate_rows:
            raise ValueError("ready report coverage evidence is empty")
        code_datasets: set[str] = set()
        for row in code_rows:
            if not isinstance(row, Mapping):
                raise ValueError("code coverage row is invalid")
            dataset = str(row.get("dataset") or "")
            code_datasets.add(dataset)
            if (
                dataset not in DELISTED_HISTORY_REQUIRED_DATASETS
                or row.get("required") is not True
                or row.get("covered") is not True
                or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("content_hash") or ""))
                or str(row.get("exchange") or "") not in {"SSE", "SZSE"}
                or int(row.get("year", 0)) not in range(2018, 2024)
                or int(row.get("row_count", -1)) < 0
            ):
                raise ValueError("code coverage row failed a hard gate")
        if code_datasets != set(DELISTED_HISTORY_REQUIRED_DATASETS):
            raise ValueError("code coverage omits a required dataset")
        aggregate_datasets: set[str] = set()
        for row in aggregate_rows:
            if not isinstance(row, Mapping):
                raise ValueError("aggregate coverage row is invalid")
            dataset = str(row.get("dataset") or "")
            aggregate_datasets.add(dataset)
            required = int(row.get("required_codes", 0))
            covered = int(row.get("covered_codes", -1))
            if (
                dataset not in DELISTED_HISTORY_REQUIRED_DATASETS
                or required <= 0
                or covered != required
                or not np.isclose(float(row.get("coverage_rate", 0.0)), 1.0)
            ):
                raise ValueError("aggregate coverage row failed a hard gate")
        if aggregate_datasets != set(DELISTED_HISTORY_REQUIRED_DATASETS):
            raise ValueError("aggregate coverage omits a required dataset")

    def _require_delisted_history_quality(
        self, *, historical_master_gate: Mapping[str, Any]
    ) -> dict[str, Any]:
        gate = self._delisted_history_gate(
            historical_master_gate=historical_master_gate
        )
        if gate.get("ready"):
            return gate
        rows = self.database.query(
            "SELECT data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        gates = _json_value(rows[0].get("data_gates_json"), {}) if rows else {}
        gates["historical_universe_master"] = dict(historical_master_gate)
        gates["delisted_history_quality"] = gate
        self.database.update_research_project(
            PROJECT_ID,
            status="BLOCKED_DATA",
            data_gates=gates,
        )
        raise ResearchDataBlockedError(
            "V4 delisted-history quality gate failed: "
            f"{gate.get('status', 'UNKNOWN')}: {gate.get('detail', '')}"
        )

    def _require_historical_universe_master(self) -> dict[str, Any]:
        gate = self._historical_universe_gate()
        if gate.get("ready"):
            return gate
        rows = self.database.query(
            "SELECT data_gates_json FROM research_projects WHERE project_id=?", (PROJECT_ID,)
        )
        gates = _json_value(rows[0].get("data_gates_json"), {}) if rows else {}
        gates["historical_universe_master"] = gate
        self.database.update_research_project(
            PROJECT_ID,
            status="BLOCKED_DATA",
            data_gates=gates,
        )
        raise ResearchDataBlockedError(
            "V4 历史证券母表门禁未通过: "
            f"{gate.get('status', 'UNKNOWN')}: {gate.get('detail', '')}"
        )

    def _v3_batches(self, *, historical_master_snapshot: str) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id='early_winner_v3'
            AND dataset=? AND status='SUCCEEDED' AND published_end<='2023-12-31'
            ORDER BY published_end""",
            (V3_FEATURE_DATASET,),
        )
        years = {int(str(row["published_end"])[:4]) for row in rows}
        if years != set(DEVELOPMENT_YEARS) or len(rows) != len(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError("V4 源 V3 特征年份不完整或重复")
        for row in rows:
            path = Path(str(row["path"]))
            if not path.exists() or _file_sha256(path) != row["content_hash"]:
                raise ResearchDataBlockedError(f"V4 源特征哈希失败: {row['batch_id']}")
            metadata = _json_value(row.get("metadata_json"), {})
            if (
                metadata.get("historical_security_master_snapshot")
                != historical_master_snapshot
            ):
                raise ResearchDataBlockedError(
                    "V4 源 V3 特征未绑定当前历史证券母表；必须重建 2018—2023 特征"
                )
        return rows

    def _trading_calendar(self) -> tuple[dict[str, Any], list[str]]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id='early_winner_v1'
            AND dataset='trading_calendar' AND status='SUCCEEDED'
            ORDER BY fetched_at DESC"""
        )
        unique = {str(row["content_hash"]): dict(row) for row in rows}
        if len(unique) != 1:
            raise ResearchDataBlockedError(
                f"V4 交易日历批次不唯一: {len(unique)}"
            )
        record = next(iter(unique.values()))
        path = Path(str(record["path"]))
        if not path.exists() or _file_sha256(path) != record["content_hash"]:
            raise ResearchDataBlockedError("V4 交易日历哈希失败")
        values = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(values, list) or not values:
            raise ResearchDataBlockedError("V4 交易日历内容无效")
        calendar = sorted({pd.Timestamp(value).date().isoformat() for value in values})
        return record, calendar

    def _v4_batches(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id=? AND dataset=?
            AND status='SUCCEEDED' AND published_end<='2023-12-31' ORDER BY published_end""",
            (PROJECT_ID, FEATURE_DATASET),
        )
        years = {int(str(row["published_end"])[:4]) for row in rows}
        if years != set(DEVELOPMENT_YEARS) or len(rows) != len(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError("V4 40日标签快照未完成或存在重复年份")
        for row in rows:
            path = Path(str(row["path"]))
            if not path.exists() or _file_sha256(path) != row["content_hash"]:
                raise ResearchDataBlockedError(f"V4 标签分片哈希失败: {row['batch_id']}")
        return rows

    def _current_v4_batches(
        self,
        *,
        historical_master_snapshot: str,
        delisted_history_manifest_hash: str,
        delisted_history_report_hash: str,
    ) -> list[dict[str, Any]]:
        """Resolve exactly one complete snapshot for the active build protocol."""
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id=? AND dataset=?
            AND status='SUCCEEDED' AND published_end<='2023-12-31' ORDER BY published_end""",
            (PROJECT_ID, FEATURE_DATASET),
        )
        project_rows = self.database.query(
            "SELECT data_gates_json FROM research_projects WHERE project_id=?",
            (PROJECT_ID,),
        )
        gates = _json_value(project_rows[0].get("data_gates_json"), {}) if project_rows else {}
        expected_snapshot = str(
            dict(gates.get("label_snapshot") or {}).get("snapshot_hash") or ""
        )
        selected: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            metadata = _json_value(row.get("metadata_json"), {})
            if metadata.get("protocol_version") != BUILD_PROTOCOL_VERSION:
                continue
            if expected_snapshot and metadata.get("snapshot_hash") != expected_snapshot:
                continue
            if (
                metadata.get("historical_security_master_snapshot")
                != historical_master_snapshot
            ):
                continue
            if (
                metadata.get("delisted_history_manifest_hash")
                != delisted_history_manifest_hash
                or metadata.get("delisted_history_report_hash")
                != delisted_history_report_hash
            ):
                continue
            row["metadata"] = metadata
            selected.append(row)
        snapshots = {
            str(item["metadata"].get("snapshot_hash") or "") for item in selected
        }
        years = {int(str(item["published_end"])[:4]) for item in selected}
        if (
            years != set(DEVELOPMENT_YEARS)
            or len(selected) != len(DEVELOPMENT_YEARS)
            or len(snapshots) != 1
            or "" in snapshots
        ):
            raise ResearchDataBlockedError(
                "V4 current-protocol label snapshot is incomplete or ambiguous"
            )
        required = {
            "planned_entry_time",
            "planned_exit_time",
            "label_matured_in_development",
            RETURN_COLUMN,
        }
        for row in selected:
            path = Path(str(row["path"]))
            if not path.exists() or _file_sha256(path) != row["content_hash"]:
                raise ResearchDataBlockedError(
                    f"V4 current-protocol shard hash failed: {row['batch_id']}"
                )
            columns = set(pd.read_parquet(path).columns)
            missing = sorted(required - columns)
            if missing:
                raise ResearchDataBlockedError(
                    f"V4 current-protocol shard schema drift: {row['batch_id']}: {missing}"
                )
        return selected

    def _build_raw_bar_files(
        self,
        client: TdxResearchHttpClient,
        codes: list[str],
        *,
        progress_callback: Any | None,
    ) -> dict[str, Path]:
        directory = (
            self.config.runtime_dir
            / "research"
            / PROJECT_ID
            / "raw_bars"
            / f"{RAW_SOURCE_PROTOCOL_VERSION}_{RAW_START}_{RAW_END}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        pending: list[str] = []
        invalid_existing: set[str] = set()
        for code in codes:
            path = directory / f"{code.replace('.', '_')}.parquet"
            metadata_path = path.with_suffix(".json")
            if path.exists() and metadata_path.exists():
                try:
                    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                    if metadata.get("start") != RAW_START or metadata.get("end") != RAW_END:
                        raise ValueError("raw cache range drift")
                    if metadata.get("protocol_version") != RAW_SOURCE_PROTOCOL_VERSION:
                        raise ValueError("raw cache protocol drift")
                    if metadata.get("fields") != list(RAW_FIELDS):
                        raise ValueError("raw cache field drift")
                    if metadata.get("code") != code:
                        raise ValueError("raw cache code drift")
                    if metadata.get("content_hash") != _file_sha256(path):
                        raise ValueError("raw cache hash failed")
                    _validate_raw_bar_frame(code, pd.read_parquet(path))
                    result[code] = path
                    continue
                except (
                    json.JSONDecodeError,
                    OSError,
                    TypeError,
                    ValueError,
                    ResearchDataBlockedError,
                ):
                    invalid_existing.add(code)
            elif path.exists() or metadata_path.exists():
                invalid_existing.add(code)
            pending.append(code)
        for offset in range(0, len(pending), 10):
            chunk = pending[offset : offset + 10]
            value = client.call(
                "get_market_data",
                {
                    "field_list": list(RAW_FIELDS),
                    "stock_list": chunk,
                    "period": "1d",
                    "start_time": RAW_START,
                    "end_time": RAW_END,
                    "count": 0,
                    "dividend_type": "none",
                    "fill_data": False,
                },
            )
            if not isinstance(value, Mapping):
                raise ResearchDataBlockedError("V4 raw-bar response is not a code mapping")
            payload = value
            missing_codes = [code for code in chunk if code not in payload]
            if missing_codes:
                raise ResearchDataBlockedError(
                    "V4 raw-bar response omitted requested codes: "
                    f"{missing_codes[:5]}"
                )
            for code in chunk:
                node = payload.get(code, {}) if isinstance(payload, Mapping) else {}
                dates = node.get("Date", []) if isinstance(node, Mapping) else []
                if not isinstance(node, Mapping) or not isinstance(dates, list) or not dates:
                    raise ResearchDataBlockedError(f"V4 raw-bar response is empty: {code}")
                for field in RAW_FIELDS:
                    field_values = node.get(field)
                    if not isinstance(field_values, list) or len(field_values) != len(dates):
                        raise ResearchDataBlockedError(
                            f"V4 raw-bar field coverage drift: {code} {field}"
                        )
                records = []
                for position, date in enumerate(dates):
                    record: dict[str, Any] = {"bar_time": pd.Timestamp(str(date))}
                    for field in RAW_FIELDS:
                        values = node.get(field, []) if isinstance(node, Mapping) else []
                        try:
                            record[field] = float(values[position])
                        except (IndexError, TypeError, ValueError) as exc:
                            raise ResearchDataBlockedError(
                                f"V4 raw-bar numeric drift: {code} {date} {field}"
                            ) from exc
                    records.append(record)
                frame = pd.DataFrame(
                    records,
                    columns=["bar_time", *RAW_FIELDS],
                )
                _validate_raw_bar_frame(code, frame)
                path = directory / f"{code.replace('.', '_')}.parquet"
                temporary = path.with_suffix(".tmp.parquet")
                frame.to_parquet(temporary, index=False)
                if (
                    path.exists()
                    and code not in invalid_existing
                    and _file_sha256(path) != _file_sha256(temporary)
                ):
                    temporary.unlink()
                    raise ResearchDataBlockedError(f"V4 未复权日线重算不一致: {code}")
                if path.exists() and code not in invalid_existing:
                    temporary.unlink()
                else:
                    temporary.replace(path)
                metadata_path = path.with_suffix(".json")
                metadata = {
                    "code": code,
                    "start": RAW_START,
                    "end": RAW_END,
                    "rows": len(frame),
                    "source": "tdx:get_market_data:dividend_type=none",
                    "protocol_version": RAW_SOURCE_PROTOCOL_VERSION,
                    "fields": list(RAW_FIELDS),
                    "content_hash": _file_sha256(path),
                }
                serialized = json.dumps(metadata, sort_keys=True, separators=(",", ":"))
                if (
                    metadata_path.exists()
                    and code not in invalid_existing
                    and metadata_path.read_text(encoding="utf-8") != serialized
                ):
                    raise ResearchDataBlockedError(f"V4 未复权日线元数据冲突: {code}")
                metadata_temporary = metadata_path.with_suffix(".tmp.json")
                metadata_temporary.write_text(serialized, encoding="utf-8")
                metadata_temporary.replace(metadata_path)
                result[code] = path
            completed = min(offset + len(chunk), len(pending))
            if completed % 100 == 0 or completed == len(pending):
                _progress(
                    progress_callback,
                    "RAW_LABEL_BARS",
                    0.05 + 0.65 * completed / max(1, len(pending)),
                    f"未复权标签日线 {completed}/{len(pending)}",
                )
        return {code: result[code] for code in codes}

    def _build_execution_status_files(
        self,
        client: TdxResearchHttpClient,
        codes: list[str],
        *,
        progress_callback: Any | None,
    ) -> dict[str, Path]:
        """Freeze sparse TDX execution/status histories used by the labeler."""
        directory = (
            self.config.runtime_dir
            / "research"
            / PROJECT_ID
            / "execution_status"
            / f"{EXECUTION_STATUS_PROTOCOL_VERSION}_{RAW_START}_{RAW_END}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        result: dict[str, Path] = {}
        pending: list[str] = []
        invalid_existing: set[str] = set()
        for code in codes:
            path = directory / f"{code.replace('.', '_')}.json"
            if path.exists():
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        payload.get("code") != code
                        or payload.get("protocol_version")
                        != EXECUTION_STATUS_PROTOCOL_VERSION
                        or payload.get("fields") != list(EXECUTION_STATUS_FIELDS)
                        or payload.get("start") != RAW_START
                        or payload.get("end") != RAW_END
                    ):
                        raise ValueError("cache identity drift")
                    content = dict(payload)
                    expected_hash = str(content.pop("content_hash", ""))
                    if expected_hash != _hash_payload(content):
                        raise ValueError("cache hash failed")
                    _validate_execution_status_values(
                        code, dict(payload.get("values") or {})
                    )
                    result[code] = path
                    continue
                except (json.JSONDecodeError, OSError, TypeError, ValueError):
                    # A crash may leave a partial cache file. Re-fetching this
                    # one immutable shard is safe; the old bytes are replaced
                    # atomically only after the new payload validates.
                    invalid_existing.add(code)
            pending.append(code)
        for offset in range(0, len(pending), 50):
            chunk = pending[offset : offset + 50]
            rpc = client.call(
                "get_gpjy_value",
                {
                    "stock_list": chunk,
                    "table_list": list(EXECUTION_STATUS_FIELDS),
                    "start_time": RAW_START,
                    "end_time": RAW_END,
                },
            )
            if not isinstance(rpc, Mapping):
                raise ResearchDataBlockedError(
                    "V4 execution-status response is not a code mapping"
                )
            missing_codes = [code for code in chunk if code not in rpc]
            if missing_codes:
                raise ResearchDataBlockedError(
                    "V4 execution-status response omitted requested codes: "
                    f"{missing_codes[:5]}"
                )
            for code in chunk:
                fields: dict[str, list[dict[str, Any]]] = {}
                for field in EXECUTION_STATUS_FIELDS:
                    # GP15: component 0 is the limit-state code and component 1
                    # is seal-order value. GP29's ST/name-change state is in 1.
                    component = 1 if field == "GP29" else 0
                    values = _field_values(rpc, field, code, component=component)
                    by_date: dict[str, float] = {}
                    dated_values: set[tuple[str, float]] = set()
                    for timestamp, value in values:
                        if not (
                            pd.Timestamp(RAW_START)
                            <= pd.Timestamp(timestamp)
                            <= pd.Timestamp(RAW_END)
                        ):
                            continue
                        date = pd.Timestamp(timestamp).date().isoformat()
                        numeric = float(value)
                        if (
                            field in {"GP15", "GP29"}
                            and date in by_date
                            and by_date[date] != numeric
                        ):
                            raise ResearchDataBlockedError(
                                f"V4 execution-status conflict: {code} {field} {date}"
                            )
                        if field in {"GP15", "GP29"}:
                            by_date.setdefault(date, numeric)
                        else:
                            # GP30/GP43 may legitimately contain multiple
                            # corporate actions on one effective date. Preserve
                            # all distinct evidence in a canonical order.
                            dated_values.add((date, numeric))
                    if field in {"GP15", "GP29"}:
                        fields[field] = [
                            {"date": date, "value": by_date[date]}
                            for date in sorted(by_date)
                        ]
                    else:
                        fields[field] = [
                            {"date": date, "value": numeric}
                            for date, numeric in sorted(dated_values)
                        ]
                _validate_execution_status_values(code, fields)
                content = {
                    "code": code,
                    "start": RAW_START,
                    "end": RAW_END,
                    "protocol_version": EXECUTION_STATUS_PROTOCOL_VERSION,
                    "fields": list(EXECUTION_STATUS_FIELDS),
                    "values": fields,
                }
                payload = {**content, "content_hash": _hash_payload(content)}
                path = directory / f"{code.replace('.', '_')}.json"
                serialized = json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                if (
                    path.exists()
                    and code not in invalid_existing
                    and path.read_text(encoding="utf-8") != serialized
                ):
                    raise ResearchDataBlockedError(
                        f"V4 execution-status cache recompute conflict: {code}"
                    )
                temporary = path.with_suffix(".tmp")
                temporary.write_text(serialized, encoding="utf-8")
                temporary.replace(path)
                result[code] = path
            completed = min(offset + len(chunk), len(pending))
            if completed % 250 == 0 or completed == len(pending):
                _progress(
                    progress_callback,
                    "EXECUTION_STATUS_HISTORY",
                    0.70 + 0.015 * completed / max(1, len(pending)),
                    f"执行状态历史 {completed}/{len(pending)}",
                )
        return {code: result[code] for code in codes}

    @staticmethod
    def _read_execution_status(path: Path) -> dict[str, pd.Series]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        result: dict[str, pd.Series] = {}
        for field, records in dict(payload.get("values") or {}).items():
            if not isinstance(records, list) or not records:
                result[str(field)] = pd.Series(dtype=float)
                continue
            series = pd.Series(
                [float(item["value"]) for item in records],
                index=pd.to_datetime([item["date"] for item in records]),
                dtype=float,
            )
            result[str(field)] = series.sort_index()
        return result

    @staticmethod
    def _execution_status_profile(files: Mapping[str, Path]) -> dict[str, Any]:
        counts = {field: 0 for field in EXECUTION_STATUS_FIELDS}
        all_empty_codes: list[str] = []
        for code, path in files.items():
            payload = json.loads(path.read_text(encoding="utf-8"))
            values = dict(payload.get("values") or {})
            _validate_execution_status_values(code, values)
            nonempty = False
            for field in EXECUTION_STATUS_FIELDS:
                if values.get(field):
                    counts[field] += 1
                    nonempty = True
            if not nonempty:
                all_empty_codes.append(str(code))
        total = len(files)
        if not total:
            raise ResearchDataBlockedError("V4 execution-status snapshot is empty")
        # GP15 is published from 2016-09-26 and should be broadly populated for
        # this liquid, listed>=120-session universe. A near-empty result is much
        # more likely an RPC field/coverage drift than a valid quiet market.
        if counts["GP15"] / total < 0.90:
            raise ResearchDataBlockedError(
                "V4 GP15 code coverage below 90%: "
                f"{counts['GP15']}/{total}"
            )
        if counts["GP30"] / total < 0.75:
            raise ResearchDataBlockedError(
                "V4 GP30 corporate-action coverage below 75%: "
                f"{counts['GP30']}/{total}"
            )
        complete_rate = (total - len(all_empty_codes)) / total
        if complete_rate < 0.99:
            raise ResearchDataBlockedError(
                "V4 execution-status complete-code coverage below 99%: "
                f"{total - len(all_empty_codes)}/{total}"
            )
        return {
            "files": total,
            "nonempty_code_counts": counts,
            "nonempty_code_rates": {
                field: counts[field] / total for field in EXECUTION_STATUS_FIELDS
            },
            "all_empty_code_count": len(all_empty_codes),
            "all_empty_codes": sorted(all_empty_codes),
            "complete_code_rate": complete_rate,
            "missing_code_policy": "EXCLUDE_FROM_DECISION_UNIVERSE",
        }

    @staticmethod
    def _read_raw_bars(path: Path) -> pd.DataFrame:
        frame = pd.read_parquet(path)
        if frame.empty:
            return frame.set_index(pd.DatetimeIndex([], name="bar_time"))
        return frame.set_index(pd.to_datetime(frame.pop("bar_time"))).sort_index()

    def _persist_execution_status_index(
        self, files: Mapping[str, Path], aggregate_hash: str
    ) -> dict[str, Any]:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "source_indexes"
        directory.mkdir(parents=True, exist_ok=True)
        profile = self._execution_status_profile(files)
        # The profile is part of the audit meaning, not the immutable sparse
        # source bytes. Give a new index policy its own content address so an
        # older, valid source index remains preserved instead of being
        # overwritten or causing a false source conflict.
        index_identity = _hash_payload(
            {
                "index_protocol": EXECUTION_STATUS_INDEX_PROTOCOL_VERSION,
                "source_protocol": EXECUTION_STATUS_PROTOCOL_VERSION,
                "aggregate_hash": aggregate_hash,
                "profile": profile,
            }
        )
        path = directory / f"{EXECUTION_STATUS_DATASET}_{index_identity}.json"
        payload = {
            "dataset": EXECUTION_STATUS_DATASET,
            "index_protocol_version": EXECUTION_STATUS_INDEX_PROTOCOL_VERSION,
            "protocol_version": EXECUTION_STATUS_PROTOCOL_VERSION,
            "aggregate_hash": aggregate_hash,
            "range": [RAW_START, RAW_END],
            "fields": list(EXECUTION_STATUS_FIELDS),
            "profile": profile,
            "files": [
                {
                    "code": code,
                    "path": str(file_path),
                    "content_hash": _file_sha256(file_path),
                }
                for code, file_path in sorted(files.items())
            ],
        }
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise ResearchDataBlockedError("V4 execution-status source-index conflict")
        path.write_text(serialized, encoding="utf-8")
        record = {
            "batch_id": f"ewv4status_{index_identity[:20]}",
            "project_id": PROJECT_ID,
            "dataset": EXECUTION_STATUS_DATASET,
            "source": "tdx:get_gpjy_value:GP15+GP29+GP30+GP43",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": "2017-12-01",
            "published_end": "2023-12-31",
            "row_count": len(files),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": _hash_payload(list(EXECUTION_STATUS_FIELDS)),
            "metadata": {
                "aggregate_hash": aggregate_hash,
                "index_protocol_version": EXECUTION_STATUS_INDEX_PROTOCOL_VERSION,
                "protocol_version": EXECUTION_STATUS_PROTOCOL_VERSION,
                "fields": list(EXECUTION_STATUS_FIELDS),
                "profile": profile,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_raw_index(
        self, files: Mapping[str, Path], aggregate_hash: str
    ) -> dict[str, Any]:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "source_indexes"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{RAW_DATASET}_{aggregate_hash}.json"
        payload = {
            "dataset": RAW_DATASET,
            "protocol_version": RAW_SOURCE_PROTOCOL_VERSION,
            "aggregate_hash": aggregate_hash,
            "range": [RAW_START, RAW_END],
            "label_only_outcome_tail": False,
            "files": [
                {"code": code, "path": str(file_path), "content_hash": _file_sha256(file_path)}
                for code, file_path in sorted(files.items())
            ],
        }
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise ResearchDataBlockedError("V4 原始日线索引内容冲突")
        path.write_text(serialized, encoding="utf-8")
        record = {
            "batch_id": f"ewv4src_{aggregate_hash[:24]}",
            "project_id": PROJECT_ID,
            "dataset": RAW_DATASET,
            "source": "tdx:127.0.0.1:17709",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": "2017-12-01",
            "published_end": "2023-12-31",
            "row_count": len(files),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": _hash_payload(list(RAW_FIELDS)),
            "metadata": {
                "aggregate_hash": aggregate_hash,
                "file_count": len(files),
                "label_only_outcome_tail": False,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_year(
        self,
        year: int,
        frame: pd.DataFrame,
        *,
        snapshot_hash: str,
        profile: Mapping[str, Any],
        historical_master_snapshot: str,
        delisted_history_manifest_hash: str,
        delisted_history_report_hash: str,
    ) -> dict[str, Any]:
        batch_id = f"ewv4f_{snapshot_hash[:20]}_{year}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "features"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        temporary = path.with_suffix(".tmp.parquet")
        frame.sort_values(["asof", "code"]).reset_index(drop=True).to_parquet(temporary, index=False)
        if path.exists():
            if _file_sha256(path) != _file_sha256(temporary):
                temporary.unlink()
                raise ResearchDataBlockedError(f"V4 冻结标签分片重算不一致: {batch_id}")
            temporary.unlink()
        else:
            temporary.replace(path)
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": FEATURE_DATASET,
            "source": "early_winner_v3_frozen+tdx_raw_40d_outcomes",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": f"{year}-01-01",
            "published_end": f"{year}-12-31",
            "row_count": len(frame),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": _hash_payload([(column, str(dtype)) for column, dtype in frame.dtypes.items()]),
            "metadata": {
                "protocol_version": BUILD_PROTOCOL_VERSION,
                "snapshot_hash": snapshot_hash,
                "historical_security_master_snapshot": historical_master_snapshot,
                "delisted_history_manifest_hash": delisted_history_manifest_hash,
                "delisted_history_report_hash": delisted_history_report_hash,
                "profile": dict(profile),
                "frozen": True,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_manifest(self, snapshot_hash: str, payload: Mapping[str, Any]) -> Path:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "history"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{snapshot_hash}.manifest.json"
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise ResearchDataBlockedError("V4 内容寻址清单冲突")
        path.write_text(serialized, encoding="utf-8")
        return path

    def _persist_audit(self, audit_id: str, payload: Mapping[str, Any]) -> Path:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "audits"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{audit_id}.json"
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        if path.exists() and path.read_text(encoding="utf-8") != serialized:
            raise ResearchDataBlockedError("V4 同协议审计结果不一致")
        path.write_text(serialized, encoding="utf-8")
        return path


def prepare_v4_labels(frame: pd.DataFrame) -> pd.DataFrame:
    data = frame.copy()
    data["year"] = pd.to_datetime(data["asof"], errors="coerce").dt.year
    data["market_breadth_ma60"] = np.nan
    data["v4_eligible"] = False
    data["target"] = np.nan
    for _, group in data.groupby("asof", sort=False):
        marked = mark_research_universe_eligibility(group.to_dict("records"))
        common = pd.Series([bool(item.get("eligible")) for item in marked], index=group.index)
        universe = pd.Series([bool(item.get("universe_gate")) for item in marked], index=group.index)
        # mark_research_universe_eligibility intentionally recomputes the
        # decision-time market gates. Preserve the independent source-quality
        # gate afterwards so a TDX code with an incomplete GP15/29/30/43 shard
        # can never silently re-enter breadth, training or evaluation.
        status_complete = group.get(
            "execution_status_complete", pd.Series(False, index=group.index)
        ).fillna(False).astype(bool)
        common &= status_complete
        universe &= status_complete
        returns = pd.to_numeric(group[RETURN_COLUMN], errors="coerce")
        above_ma60 = (
            pd.to_numeric(group["close"], errors="coerce")
            > pd.to_numeric(group["ma60"], errors="coerce")
        )
        breadth = float((above_ma60 & universe).sum() / max(1, int(universe.sum())))
        data.loc[group.index, "market_breadth_ma60"] = breadth
        # Selection eligibility must contain decision-time information only.
        # Whether the next-session order can execute is an outcome: rank the
        # complete decision pool first and leave an unfilled Top20 slot in cash.
        decision_eligible = common & (breadth > MARKET_BREADTH_THRESHOLD)
        data.loc[group.index, "v4_eligible"] = decision_eligible
        labeled = (
            decision_eligible
            & group["entry_executable"].fillna(False).astype(bool)
            & returns.notna()
        )
        if not bool(labeled.any()):
            continue
        threshold = float(returns.loc[labeled].quantile(TARGET_QUANTILE))
        target = returns.loc[labeled] >= threshold
        if TARGET_REQUIRES_POSITIVE_RETURN:
            target &= returns.loc[labeled] > 0
        data.loc[group.index[labeled], "target"] = target.astype(int)
    return data


def run_v4_development_experiment(
    frame: pd.DataFrame,
    *,
    model_directory: Path | None = None,
    source_hash: str = "test-snapshot",
    trading_calendar: Iterable[str] | None = None,
    progress_callback: Any | None = None,
) -> dict[str, Any]:
    from sklearn.ensemble import HistGradientBoostingClassifier

    data = prepare_v4_labels(frame)
    model: dict[str, Any] = {"yearly": {}}
    baseline: dict[str, Any] = {"yearly": {}}
    if model_directory is not None:
        model_directory.mkdir(parents=True, exist_ok=True)
    for year_index, test_year in enumerate(OOS_DEVELOPMENT_YEARS):
        train = data.loc[
            (data["year"] >= DEVELOPMENT_YEARS[0])
            & (data["year"] < test_year)
            & data["v4_eligible"]
            & data["target"].notna()
        ].copy()
        train = _purge_completed_outcomes(train, test_year=test_year)
        year_pool = data.loc[data["year"] == test_year].copy()
        year_pool = _embargo_by_trading_calendar(
            year_pool,
            test_year=test_year,
            trading_calendar=trading_calendar,
        )
        # Calendar weeks, including breadth-off cash weeks, define the eight
        # phases. A date is admitted only after its 40-session window matures.
        maturity_column = (
            "label_window_matured"
            if "label_window_matured" in year_pool
            else "label_matured_in_development"
        )
        if maturity_column in year_pool:
            maturity_by_date = year_pool.groupby("asof")[maturity_column].any()
            mature_dates = set(maturity_by_date.loc[maturity_by_date].index.astype(str))
        else:
            mature_dates = set(
                year_pool.loc[year_pool["target"].notna(), "asof"].astype(str)
            )
        year_pool = year_pool.loc[year_pool["asof"].astype(str).isin(mature_dates)]
        test = year_pool.loc[year_pool["v4_eligible"]].copy()
        labeled_test = test.loc[test["target"].notna()]
        if (
            train.empty
            or test.empty
            or labeled_test.empty
            or train["target"].nunique() < 2
        ):
            raise ResearchDataBlockedError(f"V4 {test_year} 缺少两类训练或测试样本")
        train_features, preprocessor = _fit_preprocessor(train, TECHNICAL_FEATURES)
        test_features = _apply_preprocessor(test, preprocessor)
        estimator = HistGradientBoostingClassifier(**MODEL_PARAMETERS)
        estimator.fit(train_features, train["target"].astype(int))
        probabilities = estimator.predict_proba(test_features)[:, 1]
        evaluated = data.loc[data["year"] == test_year].copy()
        evaluated["evaluation_eligible"] = False
        evaluated.loc[test.index, "evaluation_eligible"] = True
        evaluated["evaluation_period"] = evaluated["asof"].astype(str).isin(mature_dates)
        evaluated["score"] = np.nan
        evaluated.loc[test.index, "score"] = probabilities
        candidate_metrics, baseline_metrics = _evaluate_v4_pair(
            evaluated,
            candidate_score_column="score",
            baseline_score_column="relative_return_60",
            eligibility_column="evaluation_eligible",
        )
        candidate_metrics.update(
            {
                "worst_phase_total_return_excess": _worst_phase_excess(
                    candidate_metrics, baseline_metrics, "total_return"
                ),
                "worst_phase_double_cost_return_excess": _worst_phase_excess(
                    candidate_metrics, baseline_metrics, "double_cost_return"
                ),
                "worst_phase_drawdown_gap": _worst_phase_excess(
                    candidate_metrics, baseline_metrics, "max_drawdown"
                ),
            }
        )
        candidate_metrics["gate_passed"] = passes_v4_development_gate(
            candidate_metrics, baseline_metrics
        )
        model_bytes = pickle.dumps(
            {
                "estimator": estimator,
                "preprocessor": preprocessor,
                "features": list(train_features.columns),
                "source_hash": source_hash,
                "test_year": test_year,
                "protocol": DEVELOPMENT_PROTOCOL_VERSION,
            },
            protocol=5,
        )
        model_hash = hashlib.sha256(model_bytes).hexdigest()
        model_path = None
        if model_directory is not None:
            model_directory.mkdir(parents=True, exist_ok=True)
            model_path = model_directory / f"ewv4_{test_year}_{model_hash}.pkl"
            if not model_path.exists() or hashlib.sha256(
                model_path.read_bytes()
            ).hexdigest() != model_hash:
                temporary = model_path.with_suffix(".tmp.pkl")
                temporary.write_bytes(model_bytes)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != model_hash:
                    temporary.unlink(missing_ok=True)
                    raise ResearchDataBlockedError(
                        f"V4 {test_year} model artifact hash failed"
                    )
                temporary.replace(model_path)
        model["yearly"][str(test_year)] = {
            **candidate_metrics,
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
            "test_labeled_rows": int(len(labeled_test)),
            "target_rate_train": float(train["target"].mean()),
            "target_rate_test": float(labeled_test["target"].mean()),
            "feature_count": int(len(preprocessor)),
            "features": list(preprocessor),
            "preprocessor_hash": _hash_payload(preprocessor),
            "model_hash": model_hash,
            "model_path": str(model_path) if model_path is not None else None,
        }
        baseline["yearly"][str(test_year)] = baseline_metrics
        _progress(
            progress_callback,
            "DEVELOPMENT_OOS",
            0.20 + 0.18 * (year_index + 1),
            f"{test_year} V4 样本外折完成",
        )
    model["passed"] = all(
        bool(metrics["gate_passed"]) for metrics in model["yearly"].values()
    )
    model["passed_years"] = sum(
        bool(metrics["gate_passed"]) for metrics in model["yearly"].values()
    )
    return {
        "protocol": {
            "protocol_version": DEVELOPMENT_PROTOCOL_VERSION,
            "preprocessor_version": PREPROCESSOR_VERSION,
            "random_seed": MODEL_RANDOM_SEED,
            "model_parameters": MODEL_PARAMETERS,
            "features": list(TECHNICAL_FEATURES),
            "holding_trading_days": HOLDING_TRADING_DAYS,
            "embargo_trading_days": EMBARGO_TRADING_DAYS,
            "market_breadth_threshold": MARKET_BREADTH_THRESHOLD,
            "target_quantile": TARGET_QUANTILE,
            "target_requires_positive_return": TARGET_REQUIRES_POSITIVE_RETURN,
            "portfolio_size": PORTFOLIO_SIZE,
            "non_overlap_phases": NON_OVERLAP_PHASES,
            "minimum_phase_periods": MINIMUM_PHASE_PERIODS,
            "minimum_phase_invested_periods": MINIMUM_PHASE_INVESTED_PERIODS,
            "unfilled_slot_policy": "CASH_NO_REFILL",
            "cycle_turnover_policy": "FULL_EXIT_REBUILD_ON_FILLED_NOTIONAL",
            "paired_cycle_policy": "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY",
            "development_years": list(DEVELOPMENT_YEARS),
            "oos_years": list(OOS_DEVELOPMENT_YEARS),
            "excluded_tuning_years": [2024, 2025, 2026],
        },
        "model": model,
        "baseline": baseline,
        "environment": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "scipy": scipy.__version__,
            "pyarrow": _pyarrow_version(),
        },
    }


def _pyarrow_version() -> str:
    try:
        import pyarrow

        return str(pyarrow.__version__)
    except ImportError:
        return "unavailable"


def _purge_completed_outcomes(
    frame: pd.DataFrame, *, test_year: int
) -> pd.DataFrame:
    """Keep only training labels whose executable exit was known before test year."""
    exits = pd.to_datetime(frame.get("exit_time"), errors="coerce")
    boundary = pd.Timestamp(test_year, 1, 1)
    return frame.loc[exits.notna() & (exits < boundary)]


def _embargo_by_trading_calendar(
    frame: pd.DataFrame,
    *,
    test_year: int,
    trading_calendar: Iterable[str] | None,
) -> pd.DataFrame:
    if trading_calendar is None:
        sessions = pd.bdate_range(f"{test_year}-01-01", f"{test_year}-12-31")
    else:
        sessions = pd.DatetimeIndex(pd.to_datetime(list(trading_calendar), errors="coerce"))
        sessions = sessions[(sessions.year == test_year) & sessions.notna()]
    sessions = sessions.sort_values().unique()
    if len(sessions) < EMBARGO_TRADING_DAYS:
        raise ResearchDataBlockedError(f"V4 {test_year} 交易日历不足20个交易日")
    boundary = pd.Timestamp(sessions[EMBARGO_TRADING_DAYS - 1])
    decisions = pd.to_datetime(frame["asof"], errors="coerce")
    return frame.loc[decisions > boundary]


def _evaluate_v4_year(
    frame: pd.DataFrame,
    score_column: str,
    eligibility_column: str,
    *,
    cycle_positions: Mapping[int, list[int]] | None = None,
) -> dict[str, Any]:
    weeks, weekly_precision, ic_values = _prepare_v4_weeks(
        frame, score_column, eligibility_column
    )
    phases = [
        _evaluate_v4_phase(
            weeks,
            phase=phase,
            positions=(cycle_positions or {}).get(phase),
        )
        for phase in range(NON_OVERLAP_PHASES)
        if phase < len(weeks)
    ]
    return _summarize_v4_evaluation(
        frame,
        score_column,
        eligibility_column,
        weeks=weeks,
        phases=phases,
        weekly_precision=weekly_precision,
        ic_values=ic_values,
    )


def _prepare_v4_weeks(
    frame: pd.DataFrame, score_column: str, eligibility_column: str
) -> tuple[list[dict[str, Any]], list[float], list[float]]:
    weeks: list[dict[str, Any]] = []
    weekly_precision: list[float] = []
    ic_values: list[float] = []
    for asof, group in frame.groupby("asof", sort=True):
        evaluation_period = group.get(
            "evaluation_period", group[eligibility_column]
        ).fillna(False).astype(bool)
        if not bool(evaluation_period.any()):
            continue
        decision_mask = group[eligibility_column].fillna(False).astype(bool)
        pool = group.loc[decision_mask].copy()
        if pool.empty:
            selected = pool
            filled = pool
        else:
            score = pd.to_numeric(pool[score_column], errors="coerce")
            pool = pool.loc[score.notna() & np.isfinite(score)].copy()
            # Rank before looking at next-session execution. A failed order
            # remains cash; a lower-ranked name is never pulled into Top20.
            selected = _select_with_industry_cap(
                pool,
                score_column,
                maximum_candidates=PORTFOLIO_SIZE,
            )
            selected_target = pd.to_numeric(selected.get("target"), errors="coerce")
            weekly_precision.append(
                float((selected_target == 1).sum()) / float(PORTFOLIO_SIZE)
            )
            selected_return = pd.to_numeric(
                selected.get(RETURN_COLUMN), errors="coerce"
            )
            selected_executable = selected.get(
                "entry_executable", pd.Series(False, index=selected.index)
            ).fillna(False).astype(bool)
            filled = selected.loc[
                selected_executable & selected_return.notna()
            ].copy()

            labeled_pool = pool.loc[
                pd.to_numeric(pool.get("target"), errors="coerce").notna()
                & pd.to_numeric(pool.get(RETURN_COLUMN), errors="coerce").notna()
            ]
            if (
                len(labeled_pool) >= 3
                and pd.to_numeric(labeled_pool[score_column], errors="coerce").nunique() > 1
                and pd.to_numeric(labeled_pool[RETURN_COLUMN], errors="coerce").nunique() > 1
            ):
                correlation = labeled_pool[score_column].corr(
                    labeled_pool[RETURN_COLUMN], method="spearman"
                )
                if pd.notna(correlation):
                    ic_values.append(float(correlation))
        weeks.append(
            {
                "asof": str(asof),
                "decision_at": pd.Timestamp(asof),
                "selected": selected,
                "filled": filled,
                "planned_entry_at": _phase_boundary(
                    group,
                    "planned_entry_time",
                    fallback=pd.Timestamp(asof),
                    reducer="min",
                ),
                "planned_exit_at": _phase_boundary(
                    group,
                    "planned_exit_time",
                    fallback=pd.NaT,
                    reducer="max",
                ),
            }
        )
    return weeks, weekly_precision, ic_values


def _evaluate_v4_pair(
    frame: pd.DataFrame,
    *,
    candidate_score_column: str,
    baseline_score_column: str,
    eligibility_column: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    candidate_weeks, candidate_precision, candidate_ic = _prepare_v4_weeks(
        frame, candidate_score_column, eligibility_column
    )
    baseline_weeks, baseline_precision, baseline_ic = _prepare_v4_weeks(
        frame, baseline_score_column, eligibility_column
    )
    if [item["asof"] for item in candidate_weeks] != [
        item["asof"] for item in baseline_weeks
    ]:
        raise ResearchDataBlockedError("V4 candidate and baseline evaluation calendars differ")

    phase_positions = {
        phase: _joint_v4_phase_positions(
            candidate_weeks, baseline_weeks, phase=phase
        )
        for phase in range(NON_OVERLAP_PHASES)
        if phase < len(candidate_weeks)
    }
    candidate_phases: list[dict[str, Any]] = []
    baseline_phases: list[dict[str, Any]] = []
    for phase, positions in phase_positions.items():
        candidate_phase = _evaluate_v4_phase(
            candidate_weeks, phase=phase, positions=positions
        )
        baseline_phase = _evaluate_v4_phase(
            baseline_weeks, phase=phase, positions=positions
        )
        for position, candidate_cycle, baseline_cycle in zip(
            positions,
            candidate_phase["cycles"],
            baseline_phase["cycles"],
            strict=True,
        ):
            joint_boundary = max(
                _v4_week_capital_available_at(candidate_weeks[position]),
                _v4_week_capital_available_at(baseline_weeks[position]),
            ).isoformat()
            candidate_cycle["joint_capital_available_at"] = joint_boundary
            baseline_cycle["joint_capital_available_at"] = joint_boundary
        candidate_phases.append(candidate_phase)
        baseline_phases.append(baseline_phase)
    candidate = _summarize_v4_evaluation(
        frame,
        candidate_score_column,
        eligibility_column,
        weeks=candidate_weeks,
        phases=candidate_phases,
        weekly_precision=candidate_precision,
        ic_values=candidate_ic,
    )
    baseline = _summarize_v4_evaluation(
        frame,
        baseline_score_column,
        eligibility_column,
        weeks=baseline_weeks,
        phases=baseline_phases,
        weekly_precision=baseline_precision,
        ic_values=baseline_ic,
    )
    _assert_v4_pair_alignment(candidate, baseline)
    return candidate, baseline


def _summarize_v4_evaluation(
    frame: pd.DataFrame,
    score_column: str,
    eligibility_column: str,
    *,
    weeks: list[dict[str, Any]],
    phases: list[dict[str, Any]],
    weekly_precision: list[float],
    ic_values: list[float],
) -> dict[str, Any]:
    from sklearn.metrics import average_precision_score

    active_phases = [item for item in phases if int(item["periods"]) > 0]
    if active_phases:
        def median_metric(name: str) -> float:
            return float(np.median([float(item[name]) for item in active_phases]))

        metrics: dict[str, Any] = {
            "periods": int(min(int(item["periods"]) for item in active_phases)),
            "total_return": median_metric("total_return"),
            "double_cost_return": median_metric("double_cost_return"),
            "sharpe": median_metric("sharpe"),
            "calmar": median_metric("calmar"),
            "annualized_return": median_metric("annualized_return"),
            "max_drawdown": min(float(item["max_drawdown"]) for item in active_phases),
            "turnover": median_metric("turnover"),
            "worst_phase_total_return": min(
                float(item["total_return"]) for item in active_phases
            ),
            "worst_phase_double_cost_return": min(
                float(item["double_cost_return"]) for item in active_phases
            ),
            "worst_phase_max_drawdown": min(
                float(item["max_drawdown"]) for item in active_phases
            ),
            "best_phase_double_cost_return": max(
                float(item["double_cost_return"]) for item in active_phases
            ),
            "min_phase_periods": min(int(item["periods"]) for item in active_phases),
            "min_phase_invested_periods": min(
                int(item["invested_periods"]) for item in active_phases
            ),
            "min_phase_filled_slots": min(
                int(item["filled_slots"]) for item in active_phases
            ),
        }
    else:
        metrics = {
            **_portfolio_metrics([], [], [], periods_per_year=252.0 / HOLDING_TRADING_DAYS),
            "worst_phase_total_return": 0.0,
            "worst_phase_double_cost_return": 0.0,
            "worst_phase_max_drawdown": 0.0,
            "best_phase_double_cost_return": 0.0,
            "min_phase_periods": 0,
            "min_phase_invested_periods": 0,
            "min_phase_filled_slots": 0,
        }

    labeled = frame.loc[
        frame[eligibility_column].fillna(False).astype(bool)
        & pd.to_numeric(frame.get("target"), errors="coerce").notna()
        & pd.to_numeric(frame.get(score_column), errors="coerce").notna()
    ]
    target = pd.to_numeric(labeled.get("target"), errors="coerce").astype(int)
    probability = pd.to_numeric(labeled.get(score_column), errors="coerce")
    metrics["pr_auc"] = (
        float(average_precision_score(target, probability))
        if len(labeled) and target.nunique() > 1
        else 0.0
    )
    metrics["precision_at_20"] = (
        float(np.mean(weekly_precision)) if weekly_precision else 0.0
    )
    metrics["ic"] = float(np.mean(ic_values)) if ic_values else 0.0
    metrics["weekly_rank_periods"] = int(len(weekly_precision))
    metrics["evaluation_periods"] = int(len(weeks))
    metrics["breadth_cash_periods"] = int(
        sum(len(item["selected"]) == 0 for item in weeks)
    )
    metrics["phase_count"] = int(len(active_phases))
    metrics["phase_metrics"] = phases
    metrics["return_policy"] = "EIGHT_PHASE_NON_OVERLAPPING_FULL_EXIT_REBUILD"
    metrics["paired_cycle_policy"] = "JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY"
    metrics["unfilled_slot_policy"] = "CASH_NO_REFILL"
    metrics["cost_policy"] = "20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS"
    metrics["drawdown_policy"] = "CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0"
    metrics["annualization_policy"] = "40_SESSION_EQUIVALENT_PERIODS"
    return metrics


def _evaluate_v4_phase(
    weeks: list[dict[str, Any]],
    *,
    phase: int,
    positions: list[int] | None = None,
) -> dict[str, Any]:
    cycle_returns: list[float] = []
    cycle_turnover: list[float] = []
    cycle_details: list[dict[str, Any]] = []
    selected_positions = (
        list(positions)
        if positions is not None
        else _single_v4_phase_positions(weeks, phase=phase)
    )
    for position in selected_positions:
        week = weeks[position]
        selected = week["selected"]
        filled = week["filled"]
        filled_returns = pd.to_numeric(filled.get(RETURN_COLUMN), errors="coerce")
        filled_slots = int(filled_returns.notna().sum())
        gross_return = float(filled_returns.dropna().sum()) / float(PORTFOLIO_SIZE)
        executed_fraction = float(filled_slots) / float(PORTFOLIO_SIZE)
        cycle_returns.append(gross_return)
        # Every filled name is fully sold before the cohort is rebuilt. Code
        # overlap therefore gives no turnover credit; only actual cash slots do.
        cycle_turnover.append(executed_fraction)
        cycle_details.append(
            {
                "asof": str(week["asof"]),
                "planned_entry_at": pd.Timestamp(week["planned_entry_at"]).isoformat(),
                "capital_available_at": _v4_week_capital_available_at(week).isoformat(),
                "selected_slots": int(len(selected)),
                "filled_slots": filled_slots,
                "cash_slots": int(PORTFOLIO_SIZE - filled_slots),
                "gross_return": gross_return,
                "turnover": executed_fraction,
            }
        )

    metrics: dict[str, Any] = _portfolio_metrics(
        cycle_returns,
        [],
        cycle_turnover,
        periods_per_year=252.0 / HOLDING_TRADING_DAYS,
    )
    total_slots = len(cycle_returns) * PORTFOLIO_SIZE
    filled_slots = sum(int(item["filled_slots"]) for item in cycle_details)
    metrics.update(
        {
            "phase": int(phase),
            "cycles": cycle_details,
            "selected_slots": sum(
                int(item["selected_slots"]) for item in cycle_details
            ),
            "filled_slots": int(filled_slots),
            "invested_periods": int(
                sum(int(item["filled_slots"]) > 0 for item in cycle_details)
            ),
            "cash_slots": int(total_slots - filled_slots),
            "fill_rate": float(filled_slots / total_slots) if total_slots else 0.0,
        }
    )
    return metrics


def _v4_week_capital_available_at(week: Mapping[str, Any]) -> pd.Timestamp:
    planned_exit_at = pd.Timestamp(week["planned_exit_at"])
    filled = week["filled"]
    exits = pd.to_datetime(filled.get("exit_time"), errors="coerce")
    actual_exit_at = pd.Timestamp(exits.max()) if bool(exits.notna().any()) else pd.NaT
    boundaries = [value for value in (planned_exit_at, actual_exit_at) if pd.notna(value)]
    if not boundaries:
        raise ResearchDataBlockedError(
            f"V4 evaluation lacks a frozen planned exit at {week['asof']}"
        )
    return max(boundaries).normalize()


def _single_v4_phase_positions(
    weeks: list[dict[str, Any]], *, phase: int
) -> list[int]:
    positions: list[int] = []
    position = int(phase)
    while position < len(weeks):
        positions.append(position)
        capital_available_at = _v4_week_capital_available_at(weeks[position])
        position += 1
        while position < len(weeks):
            next_entry_at = pd.Timestamp(weeks[position]["planned_entry_at"])
            if pd.notna(next_entry_at) and next_entry_at >= capital_available_at:
                break
            position += 1
    return positions


def _joint_v4_phase_positions(
    candidate_weeks: list[dict[str, Any]],
    baseline_weeks: list[dict[str, Any]],
    *,
    phase: int,
) -> list[int]:
    positions: list[int] = []
    position = int(phase)
    while position < len(candidate_weeks):
        positions.append(position)
        capital_available_at = max(
            _v4_week_capital_available_at(candidate_weeks[position]),
            _v4_week_capital_available_at(baseline_weeks[position]),
        )
        position += 1
        while position < len(candidate_weeks):
            candidate_entry = pd.Timestamp(candidate_weeks[position]["planned_entry_at"])
            baseline_entry = pd.Timestamp(baseline_weeks[position]["planned_entry_at"])
            if (
                pd.notna(candidate_entry)
                and pd.notna(baseline_entry)
                and candidate_entry >= capital_available_at
                and baseline_entry >= capital_available_at
            ):
                break
            position += 1
    return positions


def _assert_v4_pair_alignment(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> None:
    candidate_phases = {
        int(item["phase"]): item
        for item in candidate.get("phase_metrics", [])
        if isinstance(item, Mapping)
    }
    baseline_phases = {
        int(item["phase"]): item
        for item in baseline.get("phase_metrics", [])
        if isinstance(item, Mapping)
    }
    if set(candidate_phases) != set(baseline_phases):
        raise ResearchDataBlockedError("V4 candidate and baseline phases differ")
    for phase in sorted(candidate_phases):
        candidate_cycles = candidate_phases[phase].get("cycles", [])
        baseline_cycles = baseline_phases[phase].get("cycles", [])
        candidate_horizon = [
            (
                item.get("asof"),
                item.get("planned_entry_at"),
                item.get("joint_capital_available_at"),
            )
            for item in candidate_cycles
        ]
        baseline_horizon = [
            (
                item.get("asof"),
                item.get("planned_entry_at"),
                item.get("joint_capital_available_at"),
            )
            for item in baseline_cycles
        ]
        if candidate_horizon != baseline_horizon:
            raise ResearchDataBlockedError(
                f"V4 candidate and baseline cycle horizons differ in phase {phase}"
            )


def _phase_boundary(
    frame: pd.DataFrame,
    column: str,
    *,
    fallback: pd.Timestamp,
    reducer: str,
) -> pd.Timestamp:
    if column in frame:
        values = pd.to_datetime(frame[column], errors="coerce").dropna()
        if len(values):
            return pd.Timestamp(values.min() if reducer == "min" else values.max())
    return pd.Timestamp(fallback)


def _validate_raw_bar_frame(code: str, frame: pd.DataFrame) -> None:
    required = {"bar_time", *RAW_FIELDS}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"V4 raw-bar fields missing for {code}: {missing}")
    if frame.empty:
        raise ValueError(f"V4 raw-bar frame is empty for {code}")
    timestamps = pd.to_datetime(frame["bar_time"], errors="coerce")
    if timestamps.isna().any() or timestamps.duplicated().any():
        raise ValueError(f"V4 raw-bar time grain drift for {code}")
    lower = pd.Timestamp(RAW_START)
    upper = pd.Timestamp(RAW_END)
    if (timestamps < lower).any() or (timestamps > upper).any():
        raise ValueError(f"V4 raw-bar range drift for {code}")
    numeric = frame.loc[:, list(RAW_FIELDS)].apply(pd.to_numeric, errors="coerce")
    if numeric.isna().any().any() or not np.isfinite(numeric.to_numpy()).all():
        raise ValueError(f"V4 raw-bar numeric drift for {code}")
    if (numeric[["Open", "High", "Low", "Close", "ForwardFactor"]] <= 0).any().any():
        raise ValueError(f"V4 raw-bar non-positive price/factor for {code}")
    if (numeric[["Volume", "Amount"]] < 0).any().any():
        raise ValueError(f"V4 raw-bar negative volume/amount for {code}")


def _validate_execution_status_values(
    code: str, values: Mapping[str, Any]
) -> None:
    missing = [field for field in EXECUTION_STATUS_FIELDS if field not in values]
    if missing:
        raise ValueError(f"V4 execution-status fields missing for {code}: {missing}")
    domains = {
        "GP15": {-2.0, -1.0, 0.0, 1.0, 2.0},
        "GP29": {0.0, 1.0, 2.0, 3.0, 4.0, 5.0},
    }
    for field in EXECUTION_STATUS_FIELDS:
        records = values.get(field)
        if not isinstance(records, list):
            raise ValueError(f"V4 execution-status {code} {field} is not a list")
        by_date: dict[str, float] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError(f"V4 execution-status {code} {field} row drift")
            date = pd.Timestamp(record.get("date"))
            numeric = float(record.get("value"))
            if pd.isna(date) or not np.isfinite(numeric):
                raise ValueError(f"V4 execution-status {code} {field} invalid row")
            key = date.date().isoformat()
            if (
                field in {"GP15", "GP29"}
                and key in by_date
                and by_date[key] != numeric
            ):
                raise ValueError(
                    f"V4 execution-status conflict: {code} {field} {key}"
                )
            by_date.setdefault(key, numeric)
            if field in domains and numeric not in domains[field]:
                raise ValueError(
                    f"V4 execution-status domain drift: {code} {field}={numeric}"
                )


def _worst_phase_excess(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any], metric: str
) -> float:
    def values(payload: Mapping[str, Any]) -> dict[int, float]:
        result: dict[int, float] = {}
        raw = payload.get("phase_metrics")
        if not isinstance(raw, list):
            return result
        for item in raw:
            if isinstance(item, Mapping) and "phase" in item and metric in item:
                result[int(item["phase"])] = float(item[metric])
        return result

    candidate_values = values(candidate)
    baseline_values = values(baseline)
    common = sorted(set(candidate_values) & set(baseline_values))
    if common:
        return float(
            min(candidate_values[phase] - baseline_values[phase] for phase in common)
        )
    candidate_fallback = {
        "total_return": "worst_phase_total_return",
        "double_cost_return": "worst_phase_double_cost_return",
        "max_drawdown": "worst_phase_max_drawdown",
    }[metric]
    return float(candidate.get(candidate_fallback, candidate.get(metric, 0.0))) - float(
        baseline.get(candidate_fallback, baseline.get(metric, 0.0))
    )


def passes_v4_development_gate(
    candidate: Mapping[str, Any], baseline: Mapping[str, Any]
) -> bool:
    candidate_total = float(
        candidate.get("worst_phase_total_return", candidate.get("total_return", 0.0))
    )
    baseline_total = float(
        baseline.get("worst_phase_total_return", baseline.get("total_return", 0.0))
    )
    candidate_double = float(
        candidate.get(
            "worst_phase_double_cost_return",
            candidate.get("double_cost_return", 0.0),
        )
    )
    baseline_double = float(
        baseline.get(
            "worst_phase_double_cost_return",
            baseline.get("double_cost_return", 0.0),
        )
    )
    candidate_drawdown = float(
        candidate.get("worst_phase_max_drawdown", candidate.get("max_drawdown", -1.0))
    )
    baseline_drawdown = float(
        baseline.get("worst_phase_max_drawdown", baseline.get("max_drawdown", -1.0))
    )
    total_excess = float(
        candidate.get(
            "worst_phase_total_return_excess",
            _worst_phase_excess(candidate, baseline, "total_return"),
        )
    )
    double_excess = float(
        candidate.get(
            "worst_phase_double_cost_return_excess",
            _worst_phase_excess(candidate, baseline, "double_cost_return"),
        )
    )
    drawdown_gap = float(
        candidate.get(
            "worst_phase_drawdown_gap",
            _worst_phase_excess(candidate, baseline, "max_drawdown"),
        )
    )
    return bool(
        int(candidate.get("phase_count", NON_OVERLAP_PHASES)) == NON_OVERLAP_PHASES
        and int(baseline.get("phase_count", NON_OVERLAP_PHASES)) == NON_OVERLAP_PHASES
        and int(candidate.get("min_phase_periods", 0)) >= MINIMUM_PHASE_PERIODS
        and int(baseline.get("min_phase_periods", 0)) >= MINIMUM_PHASE_PERIODS
        and int(candidate.get("min_phase_invested_periods", 0))
        >= MINIMUM_PHASE_INVESTED_PERIODS
        and int(baseline.get("min_phase_invested_periods", 0))
        >= MINIMUM_PHASE_INVESTED_PERIODS
        and float(candidate.get("precision_at_20", 0.0))
        > float(baseline.get("precision_at_20", 0.0))
        and candidate_total > baseline_total
        and candidate_double > baseline_double
        and total_excess > 0.0
        and double_excess > 0.0
        and candidate_double > 0.0
        and candidate_drawdown >= baseline_drawdown - 0.03
        and drawdown_gap >= -0.03
        and candidate_drawdown >= -0.25
    )


def profile_v4_data(frame: pd.DataFrame, *, year: int | None = None) -> dict[str, Any]:
    data = frame.copy()
    required_columns = {
        "asof",
        "code",
        "published_at",
        "effective_at",
        "universe_gate",
        "execution_status_complete",
        "entry_executable",
        "planned_entry_time",
        "entry_time",
        "planned_exit_time",
        "exit_time",
        "entry_forward_factor",
        "exit_forward_factor",
        "label_window_matured",
        "label_matured_in_development",
        RETURN_COLUMN,
        *TECHNICAL_FEATURES,
    }
    missing_required_columns = sorted(required_columns - set(data.columns))

    def values(column: str, default: Any = np.nan) -> pd.Series:
        if column in data:
            return data[column]
        return pd.Series(default, index=data.index, name=column)

    duplicate_rows = (
        int(data.duplicated(["asof", "code"]).sum())
        if {"asof", "code"}.issubset(data.columns)
        else int(len(data))
    )
    published = pd.to_datetime(values("published_at", pd.NaT), errors="coerce")
    effective = pd.to_datetime(values("effective_at", pd.NaT), errors="coerce")
    asof = _decision_timestamps(values("asof", pd.NaT))
    common = data.get("universe_gate", pd.Series(False, index=data.index)).fillna(False).astype(bool)
    status_complete = data.get(
        "execution_status_complete", pd.Series(False, index=data.index)
    ).fillna(False).astype(bool)
    common &= status_complete
    labels = pd.to_numeric(values(RETURN_COLUMN), errors="coerce")
    window_maturity = data.get(
        "label_window_matured",
        data.get("label_matured_in_development", pd.Series(True, index=data.index)),
    ).fillna(False).astype(bool)
    maturity = data.get(
        "label_matured_in_development", pd.Series(True, index=data.index)
    ).fillna(False).astype(bool)
    executable = data.get(
        "entry_executable", pd.Series(False, index=data.index)
    ).fillna(False).astype(bool)
    def timestamps(column: str) -> pd.Series:
        return pd.to_datetime(values(column, pd.NaT), errors="coerce")

    entry = timestamps("entry_time")
    exit_time = timestamps("exit_time")
    planned_entry = timestamps("planned_entry_time")
    planned_exit = timestamps("planned_exit_time")
    window_mature_common = common & window_maturity
    window_mature_denominator = int(window_mature_common.sum())
    audited_execution = executable & window_mature_common
    audited_execution_count = int(audited_execution.sum())
    entry_after_decision = bool(
        audited_execution_count
        and (entry.loc[audited_execution] > asof.loc[audited_execution]).all()
    )
    entry_on_planned_session = bool(
        audited_execution_count
        and (
            entry.loc[audited_execution].dt.normalize()
            == planned_entry.loc[audited_execution].dt.normalize()
        ).all()
    )
    exit_after_entry = bool(
        audited_execution_count
        and (exit_time.loc[audited_execution] > entry.loc[audited_execution]).all()
    )
    exit_not_before_planned = bool(
        audited_execution_count
        and (
            exit_time.loc[audited_execution].dt.normalize()
            >= planned_exit.loc[audited_execution].dt.normalize()
        ).all()
    )
    entry_factors = pd.to_numeric(values("entry_forward_factor"), errors="coerce")
    exit_factors = pd.to_numeric(values("exit_forward_factor"), errors="coerce")
    forward_factors_valid = bool(
        audited_execution_count
        and entry_factors.loc[audited_execution].notna().all()
        and exit_factors.loc[audited_execution].notna().all()
        and (entry_factors.loc[audited_execution] > 0).all()
        and (exit_factors.loc[audited_execution] > 0).all()
    )
    eligible_denominator = int(common.sum())
    result: dict[str, Any] = {
        "year": year,
        "rows": int(len(data)),
        "decision_dates": int(values("asof").nunique()),
        "codes": int(values("code").nunique()),
        "execution_status_complete_rows": int(status_complete.sum()),
        "execution_status_incomplete_rows": int((~status_complete).sum()),
        "execution_status_incomplete_codes": sorted(
            values("code", "")
            .loc[~status_complete]
            .dropna()
            .astype(str)
            .unique()
            .tolist()
        ),
        "execution_status_complete_rate": (
            float(status_complete.mean()) if len(status_complete) else 0.0
        ),
        "missing_required_columns": missing_required_columns,
        "duplicate_grain_rows": duplicate_rows,
        "label_coverage": (
            float(labels.loc[window_mature_common].notna().mean())
            if window_mature_denominator
            else 0.0
        ),
        "label_maturity_coverage": (
            float(window_maturity.loc[common].mean()) if eligible_denominator else 0.0
        ),
        "actual_exit_coverage_among_mature_windows": (
            float(maturity.loc[window_mature_common].mean())
            if window_mature_denominator
            else 0.0
        ),
        "label_median": float(labels.median()) if labels.notna().any() else 0.0,
        "label_p01": float(labels.quantile(0.01)) if labels.notna().any() else 0.0,
        "label_p99": float(labels.quantile(0.99)) if labels.notna().any() else 0.0,
        "entry_after_decision": entry_after_decision,
        "entry_on_planned_session": entry_on_planned_session,
        "exit_after_entry": exit_after_entry,
        "exit_not_before_planned": exit_not_before_planned,
        "audited_execution_rows": audited_execution_count,
        "forward_factors_valid": forward_factors_valid,
        "timing_audit_passed": bool(
            not missing_required_columns
            and duplicate_rows == 0
            and audited_execution_count > 0
            and not asof.isna().any()
            and not published.isna().any()
            and not effective.isna().any()
            and not (published > asof).any()
            and not (effective > asof).any()
            and entry_after_decision
            and entry_on_planned_session
            and exit_after_entry
            and exit_not_before_planned
            and forward_factors_valid
        ),
        "feature_missing_rates": {
            column: float(pd.to_numeric(values(column), errors="coerce").isna().mean())
            for column in TECHNICAL_FEATURES
        },
    }
    if "target" in data:
        target = pd.to_numeric(data["target"], errors="coerce")
        result["target_rate"] = float(target.mean()) if target.notna().any() else 0.0
    return result


def _source_snapshot_hash(batches: Iterable[Mapping[str, Any]]) -> str:
    return _hash_payload(
        [
            {
                "batch_id": item["batch_id"],
                "content_hash": item["content_hash"],
                "schema_hash": item["schema_hash"],
                "row_count": item["row_count"],
            }
            for item in batches
        ]
    )


__all__ = [
    "DEVELOPMENT_PROTOCOL_VERSION",
    "EarlyWinnerV4ResearchService",
    "HOLDING_TRADING_DAYS",
    "MARKET_BREADTH_THRESHOLD",
    "TARGET_QUANTILE",
    "passes_v4_development_gate",
    "prepare_v4_labels",
    "profile_v4_data",
    "run_v4_development_experiment",
]
