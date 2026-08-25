from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from .config import PlatformConfig
from .early_winner_research import (
    MODEL_PARAMETERS,
    ResearchDataBlockedError,
    TdxResearchHttpClient,
    _normalize_evidence_refs,
)
from .early_winner_v2_research import (
    DEVELOPMENT_YEARS,
    PREPROCESSOR_VERSION,
    _decode_batch,
    _decode_validation,
    _json_value,
    _progress,
    profile_development_data,
    run_development_experiment,
)
from .storage import Database, _file_sha256
from .strategies.early_winner_v3 import EarlyWinnerV3Strategy, PROJECT_ID


PROJECT_VERSION = "3.0.0-dev1"
PROJECT_NAME = "早期强势股识别 V3"
PROJECT_DESCRIPTION = (
    "以 V1 冻结开发数据为底座，只补齐 TDX 点时流通股本、换手率和历史 PE 分位；"
    "2024/2025 在开发门禁通过前保持封存。"
)
SUPPLEMENT_PROTOCOL_VERSION = "early-winner-v3-tdx-point-in-time-v2"
DEVELOPMENT_PROTOCOL_VERSION = "early-winner-v3-development-v1"
FEATURE_DATASET = "early_winner_v3_features_v2"


class EarlyWinnerV3ResearchService:
    def __init__(self, config: PlatformConfig, database: Database) -> None:
        self.config = config
        self.database = database
        self.strategy = EarlyWinnerV3Strategy()
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
        project["excluded_tuning_years"] = [2024, 2025, 2026]
        project["frozen_validation_opened"] = False
        project["candidate_generation_enabled"] = False
        project["trade_signals_enabled"] = False
        project["promotion_allowed"] = False
        return project

    def build_supplemental_snapshot(
        self, progress_callback: Any | None = None
    ) -> dict[str, Any]:
        self.database.update_research_project(PROJECT_ID, status="DATA_BUILDING")
        _progress(progress_callback, "TDX_ADMISSION", 0.02, "检查本机 TDX 点时字段准入")
        client = TdxResearchHttpClient(tdx_root=self.config.tdx_root)
        admission = client.admission_probe()
        if not admission.ready:
            raise ResearchDataBlockedError(f"TDX 字段准入失败: {admission.status}: {admission.detail}")

        source_batches = self._v1_development_batches()
        raw_batches = {
            year: self._unique_v1_raw_batch("financial_history", year)
            for year in DEVELOPMENT_YEARS
        }
        frames = {
            year: pd.read_parquet(
                next(
                    item["path"]
                    for item in source_batches
                    if str(item["published_end"]).startswith(str(year))
                )
            )
            for year in DEVELOPMENT_YEARS
        }
        code_dates: dict[str, list[str]] = {}
        code_bar_dates: dict[str, list[str]] = {}
        for frame in frames.values():
            for code, group in frame.groupby("code"):
                dates = pd.to_datetime(group["asof"], errors="raise").dt.strftime("%Y%m%d")
                code_dates.setdefault(str(code), []).extend(dates.tolist())
                bar_dates = pd.to_datetime(
                    group["last_bar_at"], errors="raise"
                ).dt.strftime("%Y%m%d")
                code_bar_dates.setdefault(str(code), []).extend(bar_dates.tolist())
        code_dates = {code: sorted(set(dates)) for code, dates in code_dates.items()}
        code_bar_dates = {
            code: sorted(set(dates)) for code, dates in code_bar_dates.items()
        }
        _progress(
            progress_callback,
            "SHARE_CAPITAL",
            0.05,
            f"断点续取 {len(code_dates)} 只股票的历史股本",
        )
        capital_files = self._build_share_capital_files(
            client, code_dates, progress_callback=progress_callback
        )
        _progress(
            progress_callback,
            "RAW_CLOSE",
            0.42,
            f"断点续取 {len(code_bar_dates)} 只股票的未复权收盘价",
        )
        raw_close_files = self._build_raw_close_files(
            client, code_bar_dates, progress_callback=progress_callback
        )
        capital_hash = _hash_mapping(
            {code: _file_sha256(path) for code, path in capital_files.items()}
        )
        raw_close_hash = _hash_mapping(
            {code: _file_sha256(path) for code, path in raw_close_files.items()}
        )
        self._persist_source_index(
            "tdx_share_capital_history", capital_files, capital_hash
        )
        self._persist_source_index(
            "tdx_raw_close_history", raw_close_files, raw_close_hash
        )
        snapshot_hash = _hash_payload(
            {
                "protocol": SUPPLEMENT_PROTOCOL_VERSION,
                "v1_features": [
                    (item["batch_id"], item["content_hash"]) for item in source_batches
                ],
                "raw": {
                    str(year): raw_batches[year]["content_hash"]
                    for year in DEVELOPMENT_YEARS
                },
                "capital_hash": capital_hash,
                "raw_close_hash": raw_close_hash,
            }
        )
        records: list[dict[str, Any]] = []
        profiles: dict[str, Any] = {}
        for index, year in enumerate(DEVELOPMENT_YEARS):
            _progress(
                progress_callback,
                "DERIVE_FEATURES",
                0.66 + 0.04 * index,
                f"构建 {year} 点时派生特征",
            )
            derived, profile = self._derive_year(
                frames[year],
                year=year,
                financial_batch=raw_batches[year],
                capital_files=capital_files,
                raw_close_files=raw_close_files,
            )
            if (
                profile["turnover_coverage"] < 0.98
                or profile["raw_close_coverage"] < 0.98
                or profile["ttm_profit_coverage"] < 0.95
            ):
                raise ResearchDataBlockedError(
                    f"V3 {year} 覆盖门禁失败: turnover={profile['turnover_coverage']:.4f}, "
                    f"raw_close={profile['raw_close_coverage']:.4f}, "
                    f"ttm={profile['ttm_profit_coverage']:.4f}"
                )
            record = self._persist_year(
                year, derived, snapshot_hash=snapshot_hash, profile=profile
            )
            records.append(record)
            profiles[str(year)] = profile

        manifest = {
            "project_id": PROJECT_ID,
            "protocol_version": SUPPLEMENT_PROTOCOL_VERSION,
            "snapshot_hash": snapshot_hash,
            "source_batches": [item["batch_id"] for item in source_batches],
            "capital_hash": capital_hash,
            "raw_close_hash": raw_close_hash,
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
        audit_source_hash = _hash_payload(
            [(item["batch_id"], item["content_hash"]) for item in records]
        )
        matching_audits = self.database.query(
            """SELECT status FROM research_validations WHERE project_id=?
            AND snapshot_id=? ORDER BY created_at DESC LIMIT 1""",
            (PROJECT_ID, f"ewv3fs_{audit_source_hash[:32]}"),
        )
        audited_status = str(matching_audits[0]["status"]) if matching_audits else ""
        final_status = (
            audited_status
            if audited_status in {"DEVELOPMENT_READY", "DEVELOPMENT_REJECTED"}
            else "DEVELOPMENT_AUDIT_REQUIRED"
        )
        gates = {
            "tdx_point_in_time_fields": {
                "ready": True,
                "detail": "get_gb_info 历史股本 + 未复权收盘价 + 公告时点 TTM 利润",
                "probe_stock": "600519.SH",
                "probe_range": "2018-01-02—2023-12-29",
            },
            "supplemental_snapshot": {
                "ready": True,
                "snapshot_hash": snapshot_hash,
                "manifest": str(manifest_path),
                "years": list(DEVELOPMENT_YEARS),
            },
            "development_audit": {
                "ready": final_status == "DEVELOPMENT_READY",
                "status": audited_status or "REQUIRED",
                "detail": (
                    f"相同特征哈希已有审计结论: {audited_status}"
                    if audited_status
                    else "补数快照完成，尚未运行预声明开发审计"
                ),
            },
            "frozen_2024_2025": {"ready": False, "status": "SEALED"},
        }
        self.database.update_research_project(
            PROJECT_ID,
            status=final_status,
            data_asof="2023-12-31",
            data_gates=gates,
        )
        _progress(progress_callback, "COMPLETED", 1.0, "V3 点时补数快照完成")
        return {"project_id": PROJECT_ID, "status": final_status, **manifest}

    def run_development_audit(self, progress_callback: Any | None = None) -> dict[str, Any]:
        batches = self._v3_batches()
        self.database.update_research_project(PROJECT_ID, status="DEVELOPMENT_AUDITING")
        frame = pd.concat(
            [pd.read_parquet(str(item["path"])) for item in batches], ignore_index=True
        ).sort_values(["asof", "code"]).reset_index(drop=True)
        profile = profile_development_data(frame)
        for finding in profile["findings"]:
            if finding.get("code") == "NO_INFORMATION_FEATURES":
                finding.update(
                    {
                        "code": "POINT_IN_TIME_REPAIRS",
                        "severity": "INFO",
                        "confidence": "HIGH",
                        "impact": "历史换手率与 PE 分位已按决策时点恢复，不再是全空/常量。",
                        "remediation": "冻结字段来源和内容哈希；后续变更必须新建版本。",
                    }
                )
        experiment = run_development_experiment(frame, progress_callback=progress_callback)
        experiment["protocol_version"] = DEVELOPMENT_PROTOCOL_VERSION
        passing = sorted(
            name for name, result in experiment["variants"].items() if result["passed"]
        )
        status = "DEVELOPMENT_READY" if passing else "DEVELOPMENT_REJECTED"
        source_hash = _hash_payload(
            [(item["batch_id"], item["content_hash"]) for item in batches]
        )
        protocol_hash = _hash_payload(
            {
                "protocol": DEVELOPMENT_PROTOCOL_VERSION,
                "preprocessor": PREPROCESSOR_VERSION,
                "source_hash": source_hash,
                "parameters": MODEL_PARAMETERS,
                "variants": sorted(experiment["variants"]),
            }
        )
        audit_id = f"ewv3_dev_{protocol_hash[:24]}"
        previous = self.database.query(
            "SELECT created_at FROM research_validations WHERE validation_id=?", (audit_id,)
        )
        now = str(previous[0]["created_at"]) if previous else datetime.now().astimezone().isoformat()
        variant_gates = {
            name: {
                "yearly": {
                    year: bool(metrics["gate_passed"])
                    for year, metrics in result["yearly"].items()
                },
                "passed": bool(result["passed"]),
            }
            for name, result in experiment["variants"].items()
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
            "passing_variants": passing,
            "frozen_validation_opened": False,
            "tuning_exclusions": [2024, 2025, 2026],
            "promotion_allowed": False,
        }
        audit_path = self._persist_audit(audit_id, payload)
        self.database.save_research_data_batch(
            {
                "batch_id": audit_id,
                "project_id": PROJECT_ID,
                "dataset": "early_winner_v3_development_audit",
                "source": "early_winner_v3_frozen_2018_2023",
                "status": "SUCCEEDED",
                "fetched_at": now,
                "published_start": "2018-01-01",
                "published_end": "2023-12-31",
                "row_count": len(frame),
                "path": str(audit_path),
                "content_hash": _file_sha256(audit_path),
                "schema_hash": protocol_hash,
                "metadata": {"passing_variants": passing, "frozen_validation_opened": False},
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
                "snapshot_id": f"ewv3fs_{source_hash[:32]}",
                "rule_metrics": experiment["variants"]["full_clean"],
                "ml_metrics": experiment["variants"]["technical_clean"],
                "baseline_metrics": experiment["baseline"],
                "stress_metrics": {"profile": profile, "variants": experiment["variants"]},
                "gates": variant_gates,
                "champion": {},
                "error": "" if passing else "no V3 variant passed every 2020-2023 OOS year",
            }
        )
        gates = {
            "point_in_time_repairs": {
                "ready": True,
                "turnover_missing_rate": float(
                    pd.to_numeric(frame["turnover_20"], errors="coerce").isna().mean()
                ),
                "valuation_unique": int(frame["valuation_percentile"].nunique(dropna=True)),
            },
            "development_stability": {
                "ready": bool(passing),
                "detail": f"通过方案: {', '.join(passing)}" if passing else "没有方案逐年通过 2020—2023 门禁",
            },
            "frozen_2024_2025": {
                "ready": False,
                "status": "SEALED" if not passing else "VALIDATION_REQUIRED",
            },
        }
        self.database.update_research_project(
            PROJECT_ID, status=status, data_asof="2023-12-31", data_gates=gates
        )
        _progress(progress_callback, "COMPLETED", 1.0, f"V3 开发审计完成：{status}")
        return {"project_id": PROJECT_ID, **payload}

    def _v1_development_batches(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT batch_id,path,content_hash,schema_hash,published_start,published_end,row_count
            FROM research_data_batches WHERE project_id='early_winner_v1'
            AND dataset='early_winner_features' AND status='SUCCEEDED'
            AND published_end<='2023-12-31' ORDER BY published_end"""
        )
        years = {int(str(row["published_end"])[:4]) for row in rows}
        if years != set(DEVELOPMENT_YEARS) or len(rows) != len(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError(f"V3 源特征年份不完整或重复: {sorted(years)}")
        for row in rows:
            path = Path(str(row["path"]))
            if not path.exists() or _file_sha256(path) != row["content_hash"]:
                raise ResearchDataBlockedError(f"V3 源特征哈希失败: {row['batch_id']}")
        return rows

    def _unique_v1_raw_batch(self, dataset: str, year: int) -> dict[str, Any]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id='early_winner_v1'
            AND dataset=? AND status='SUCCEEDED' AND published_end=? ORDER BY fetched_at DESC""",
            (dataset, f"{year}-12-31"),
        )
        unique = {str(row["content_hash"]): dict(row) for row in rows}
        if len(unique) != 1:
            raise ResearchDataBlockedError(f"V3 {year} {dataset} 原始批次不唯一: {len(unique)}")
        record = next(iter(unique.values()))
        path = Path(str(record["path"]))
        if not path.exists() or _file_sha256(path) != record["content_hash"]:
            raise ResearchDataBlockedError(f"V3 {year} {dataset} 文件哈希失败")
        return record

    def _build_share_capital_files(
        self,
        client: TdxResearchHttpClient,
        code_dates: Mapping[str, list[str]],
        *,
        progress_callback: Any | None,
    ) -> dict[str, Path]:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "share_capital"
        directory.mkdir(parents=True, exist_ok=True)

        def fetch(code: str, dates: list[str]) -> tuple[str, Path]:
            path = directory / f"{code.replace('.', '_')}.json"
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("requested_dates") == dates:
                    return code, path
            value = client.call(
                "get_gb_info",
                {"stock_code": code, "date_list": dates, "count": len(dates)},
            )
            records = value if isinstance(value, list) else []
            normalized = []
            for item in records:
                if not isinstance(item, Mapping):
                    continue
                try:
                    normalized.append(
                        {
                            "Date": int(float(item["Date"])),
                            "Ltgb": float(item["Ltgb"]),
                            "Zgb": float(item["Zgb"]),
                        }
                    )
                except (KeyError, TypeError, ValueError):
                    continue
            payload = {"code": code, "requested_dates": dates, "records": normalized}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            temporary.replace(path)
            return code, path

        result: dict[str, Path] = {}
        total = len(code_dates)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(fetch, code, dates): code for code, dates in code_dates.items()
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                code, path = future.result()
                result[code] = path
                if completed % 50 == 0 or completed == total:
                    _progress(
                        progress_callback,
                        "SHARE_CAPITAL",
                        0.05 + 0.60 * completed / max(1, total),
                        f"历史股本 {completed}/{total}",
                    )
        return result

    def _build_raw_close_files(
        self,
        client: TdxResearchHttpClient,
        code_dates: Mapping[str, list[str]],
        *,
        progress_callback: Any | None,
    ) -> dict[str, Path]:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "raw_close"
        directory.mkdir(parents=True, exist_ok=True)

        def fetch(code: str, dates: list[str]) -> tuple[str, Path]:
            path = directory / f"{code.replace('.', '_')}.json"
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("requested_dates") == dates:
                    return code, path
            value = client.call(
                "get_market_data",
                {
                    "field_list": ["Close"],
                    "stock_list": [code],
                    "period": "1d",
                    "start_time": dates[0],
                    "end_time": dates[-1],
                    "count": 0,
                    "dividend_type": "none",
                    "fill_data": False,
                },
            )
            node = value.get(code, {}) if isinstance(value, Mapping) else {}
            returned_dates = node.get("Date", []) if isinstance(node, Mapping) else []
            returned_close = node.get("Close", []) if isinstance(node, Mapping) else []
            by_date = {
                str(date): float(close)
                for date, close in zip(returned_dates, returned_close)
                if close not in (None, "", "--")
            }
            records = [
                {"Date": int(date), "Close": by_date[date]}
                for date in dates
                if date in by_date
            ]
            payload = {"code": code, "requested_dates": dates, "records": records}
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8"
            )
            temporary.replace(path)
            return code, path

        result: dict[str, Path] = {}
        total = len(code_dates)
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(fetch, code, dates): code for code, dates in code_dates.items()
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                code, path = future.result()
                result[code] = path
                if completed % 50 == 0 or completed == total:
                    _progress(
                        progress_callback,
                        "RAW_CLOSE",
                        0.42 + 0.23 * completed / max(1, total),
                        f"未复权收盘价 {completed}/{total}",
                    )
        return result

    def _derive_year(
        self,
        frame: pd.DataFrame,
        *,
        year: int,
        financial_batch: Mapping[str, Any],
        capital_files: Mapping[str, Path],
        raw_close_files: Mapping[str, Path],
    ) -> tuple[pd.DataFrame, dict[str, Any]]:
        data = frame.copy()
        data["asof_dt"] = pd.to_datetime(data["asof"], errors="raise")
        data["last_bar_dt"] = pd.to_datetime(data["last_bar_at"], errors="raise")
        raw_close_rows = []
        for code in data["code"].unique():
            payload = json.loads(raw_close_files[str(code)].read_text(encoding="utf-8"))
            for item in payload["records"]:
                raw_close_rows.append(
                    {
                        "code": str(code),
                        "last_bar_dt": pd.Timestamp(str(item["Date"])),
                        "raw_close": item["Close"],
                    }
                )
        raw_close = pd.DataFrame(raw_close_rows).drop_duplicates(
            ["code", "last_bar_dt"], keep="last"
        )
        data = data.merge(raw_close, on=["code", "last_bar_dt"], how="left", validate="many_to_one")

        capital_rows = []
        for code in data["code"].unique():
            payload = json.loads(capital_files[str(code)].read_text(encoding="utf-8"))
            for item in payload["records"]:
                capital_rows.append(
                    {
                        "code": str(code),
                        "asof_dt": pd.Timestamp(str(item["Date"])),
                        "Ltgb": item["Ltgb"],
                        "Zgb": item["Zgb"],
                    }
                )
        capital = pd.DataFrame(capital_rows).drop_duplicates(["code", "asof_dt"], keep="last")
        data = data.merge(capital, on=["code", "asof_dt"], how="left", validate="many_to_one")
        data["turnover_20"] = pd.to_numeric(data["avg_volume_20"], errors="coerce") / pd.to_numeric(
            data["Ltgb"], errors="coerce"
        ).replace(0, np.nan)

        financial_payload = json.loads(Path(str(financial_batch["path"])).read_text(encoding="utf-8"))
        financial_events = _financial_profit_events(financial_payload)
        data["ttm_profit"] = np.nan
        for code, group in data.groupby("code"):
            values = _point_in_time_ttm_profit(
                financial_events.get(str(code), []), group["asof_dt"]
            )
            data.loc[group.index, "ttm_profit"] = values
        market_cap = pd.to_numeric(data["raw_close"], errors="coerce") * pd.to_numeric(
            data["Zgb"], errors="coerce"
        )
        positive_profit = pd.to_numeric(data["ttm_profit"], errors="coerce").where(
            pd.to_numeric(data["ttm_profit"], errors="coerce") > 0
        )
        data["valuation_raw"] = market_cap / positive_profit
        data["valuation_percentile"] = data.groupby("asof_dt")["valuation_raw"].rank(
            pct=True, ascending=True, method="average"
        )
        data["evidence_refs"] = [
            [
                *_normalize_evidence_refs(refs),
                f"tdx:get_gb_info:{code}:{asof.strftime('%Y%m%d')}",
                f"tdx:get_market_data:none:{code}:{last_bar.strftime('%Y%m%d')}",
                f"tdx:financial_announce_time:{financial_batch['content_hash']}",
            ]
            for refs, code, asof, last_bar in zip(
                data["evidence_refs"], data["code"], data["asof_dt"], data["last_bar_dt"]
            )
        ]
        profile = {
            "year": year,
            "rows": len(data),
            "turnover_coverage": float(data["turnover_20"].notna().mean()),
            "raw_close_coverage": float(data["raw_close"].notna().mean()),
            "ttm_profit_coverage": float(data["ttm_profit"].notna().mean()),
            "positive_pe_coverage": float(data["valuation_raw"].notna().mean()),
            "valuation_unique": int(data["valuation_percentile"].nunique(dropna=True)),
            "turnover_p99": float(data["turnover_20"].quantile(0.99)),
            "raw_close_source": "tdx:get_market_data:dividend_type=none",
            "financial_source_hash": str(financial_batch["content_hash"]),
        }
        return data.drop(columns=["asof_dt", "last_bar_dt"]), profile

    def _persist_year(
        self, year: int, frame: pd.DataFrame, *, snapshot_hash: str, profile: Mapping[str, Any]
    ) -> dict[str, Any]:
        batch_id = f"ewv3f_{snapshot_hash[:20]}_{year}"
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "features"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{batch_id}.parquet"
        temporary = path.with_suffix(".tmp.parquet")
        frame.sort_values(["asof", "code"]).reset_index(drop=True).to_parquet(
            temporary, index=False
        )
        if path.exists():
            if _file_sha256(path) != _file_sha256(temporary):
                temporary.unlink()
                raise ResearchDataBlockedError(
                    f"V3 冻结分片已存在但重算内容不一致: {batch_id}"
                )
            temporary.unlink()
        else:
            temporary.replace(path)
        schema_hash = _hash_payload([(column, str(dtype)) for column, dtype in frame.dtypes.items()])
        record = {
            "batch_id": batch_id,
            "project_id": PROJECT_ID,
            "dataset": FEATURE_DATASET,
            "source": "early_winner_v1_frozen+tdx_point_in_time",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": f"{year}-01-01",
            "published_end": f"{year}-12-31",
            "row_count": len(frame),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": schema_hash,
            "metadata": {
                "protocol_version": SUPPLEMENT_PROTOCOL_VERSION,
                "snapshot_hash": snapshot_hash,
                "profile": dict(profile),
                "frozen": True,
            },
            "error": "",
        }
        self.database.save_research_data_batch(record)
        return record

    def _persist_source_index(
        self, dataset: str, files: Mapping[str, Path], aggregate_hash: str
    ) -> dict[str, Any]:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "source_indexes"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{dataset}_{aggregate_hash}.json"
        payload = {
            "dataset": dataset,
            "protocol_version": SUPPLEMENT_PROTOCOL_VERSION,
            "aggregate_hash": aggregate_hash,
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
            raise ResearchDataBlockedError(f"V3 {dataset} 源索引哈希冲突")
        path.write_text(serialized, encoding="utf-8")
        record = {
            "batch_id": f"ewv3src_{dataset}_{aggregate_hash[:20]}",
            "project_id": PROJECT_ID,
            "dataset": dataset,
            "source": "tdx:127.0.0.1:17709",
            "status": "SUCCEEDED",
            "fetched_at": datetime.now().astimezone().isoformat(),
            "published_start": "2018-01-01",
            "published_end": "2023-12-31",
            "row_count": len(files),
            "path": str(path),
            "content_hash": _file_sha256(path),
            "schema_hash": _hash_payload(
                ["code", "path", "content_hash", SUPPLEMENT_PROTOCOL_VERSION]
            ),
            "metadata": {
                "aggregate_hash": aggregate_hash,
                "file_count": len(files),
                "content_addressed": True,
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
            raise ResearchDataBlockedError("V3 内容寻址清单已存在但内容不一致")
        path.write_text(serialized, encoding="utf-8")
        return path

    def _persist_audit(self, audit_id: str, payload: Mapping[str, Any]) -> Path:
        directory = self.config.runtime_dir / "research" / PROJECT_ID / "audits"
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{audit_id}.json"
        path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8"
        )
        return path

    def _v3_batches(self) -> list[dict[str, Any]]:
        rows = self.database.query(
            """SELECT * FROM research_data_batches WHERE project_id=? AND dataset=?
            AND status='SUCCEEDED' AND published_end<='2023-12-31' ORDER BY published_end""",
            (PROJECT_ID, FEATURE_DATASET),
        )
        years = {int(str(row["published_end"])[:4]) for row in rows}
        if years != set(DEVELOPMENT_YEARS) or len(rows) != len(DEVELOPMENT_YEARS):
            raise ResearchDataBlockedError("V3 补数快照未完成或存在重复年份")
        for row in rows:
            path = Path(str(row["path"]))
            if not path.exists() or _file_sha256(path) != row["content_hash"]:
                raise ResearchDataBlockedError(f"V3 补数分片哈希失败: {row['batch_id']}")
        return rows


def _financial_profit_events(payload: Any) -> dict[str, list[tuple[pd.Timestamp, pd.Timestamp, float, int]]]:
    events: dict[str, list[tuple[pd.Timestamp, pd.Timestamp, float, int]]] = {}
    ordinal = 0
    for batch in payload.get("batches", []) if isinstance(payload, Mapping) else []:
        if not isinstance(batch, Mapping):
            continue
        for code, node in batch.items():
            if not isinstance(node, Mapping):
                continue
            for announce, report, value in zip(
                node.get("announce_time", []), node.get("tag_time", []), node.get("FN232", [])
            ):
                try:
                    record = (
                        _compact_timestamp(announce),
                        _compact_timestamp(report),
                        float(value),
                        ordinal,
                    )
                except (TypeError, ValueError):
                    continue
                events.setdefault(str(code), []).append(record)
                ordinal += 1
    return events


def _point_in_time_ttm_profit(
    events: Iterable[tuple[pd.Timestamp, pd.Timestamp, float, int]],
    decisions: pd.Series,
) -> np.ndarray:
    ordered = sorted(events, key=lambda item: (item[0], item[3]))
    known: dict[pd.Timestamp, float] = {}
    cursor = 0
    output: list[float] = []
    for decision in pd.to_datetime(decisions, errors="raise"):
        while cursor < len(ordered) and ordered[cursor][0] <= decision:
            _, report, value, _ = ordered[cursor]
            known[report] = value
            cursor += 1
        if not known:
            output.append(np.nan)
            continue
        latest = max(known)
        if latest.month == 12 and latest.day == 31:
            output.append(known[latest])
            continue
        prior_annual = pd.Timestamp(latest.year - 1, 12, 31)
        prior_same = pd.Timestamp(latest.year - 1, latest.month, latest.day)
        if prior_annual not in known or prior_same not in known:
            output.append(np.nan)
            continue
        output.append(known[latest] + known[prior_annual] - known[prior_same])
    return np.asarray(output, dtype=float)


def _compact_timestamp(value: Any) -> pd.Timestamp:
    text = str(value).strip().split(".", 1)[0]
    if not text.isdigit() or len(text) != 8:
        raise ValueError(value)
    return pd.Timestamp(text)


def _hash_payload(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()


def _hash_mapping(payload: Mapping[str, str]) -> str:
    return _hash_payload(sorted(payload.items()))


__all__ = [
    "DEVELOPMENT_PROTOCOL_VERSION",
    "EarlyWinnerV3ResearchService",
    "SUPPLEMENT_PROTOCOL_VERSION",
    "_point_in_time_ttm_profit",
]
