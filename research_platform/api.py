from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from .ai_research import AIResearchService
from .backtest_engine import BacktestService
from .composition import (
    CompositionMode,
    ConflictPolicy,
    StrategyGroupDefinition,
    StrategyGroupMember,
)
from .config import PlatformConfig
from .data_cache import DataCacheManager
from .early_winner_research import (
    ResearchDataBlockedError,
)
from .strategies.early_winner_v2 import PROJECT_ID as EARLY_WINNER_V2_PROJECT_ID
from .strategies.early_winner_v3 import PROJECT_ID as EARLY_WINNER_V3_PROJECT_ID
from .strategies.early_winner_v4 import PROJECT_ID as EARLY_WINNER_V4_PROJECT_ID
from .strategies.early_winner_v5 import PROJECT_ID as EARLY_WINNER_V5_PROJECT_ID
from .feedback import FeedbackService
from .jobs import JobManager
from .models import SignalStatus
from .service import PlatformService
from .strategy_lab import StrategyLabService


class ScanRequest(BaseModel):
    strategies: list[str] = Field(default_factory=lambda: ["course49_system"])
    mode: Literal["research", "paper"] = "research"
    push_tdx: bool = False
    refresh_sectors: bool = False
    max_stocks: int | None = Field(default=None, ge=1)
    sampling_mode: Literal["full", "stratified"] = "full"
    sample_seed: int = 49
    refresh_data: bool = False


class BacktestRequest(BaseModel):
    strategy_id: str = Field(default="course49_system", min_length=1, max_length=64)
    start_date: str | None = None
    end_date: str | None = None
    daily_bars: int = Field(default=180, ge=90, le=2000)
    max_stocks: int | None = Field(default=None, ge=1)
    universe: Literal[
        "all_a", "main_board", "growth", "star", "beijing", "all_us",
        "sp500_ivv_proxy_v1", "custom"
    ] = "all_a"
    stock_codes: list[str] = Field(default_factory=list, max_length=500)
    refresh_sectors: bool = False
    sampling_mode: Literal["full", "stratified"] = "full"
    sample_seed: int = 49
    execution_cost_multiplier: float = Field(default=1.0, ge=0.0, le=5.0)
    refresh_data: bool = False
    playbook_ids: list[str] = Field(default_factory=list, max_length=20)
    pit_release_id: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )

    @model_validator(mode="after")
    def validate_strict_us_contract(self) -> "BacktestRequest":
        if self.strategy_id != "us_momentum_v1":
            return self
        if self.universe != "sp500_ivv_proxy_v1":
            raise ValueError("us_momentum_v1 requires sp500_ivv_proxy_v1")
        if not self.pit_release_id:
            raise ValueError("us_momentum_v1 requires a DATA_READY pit_release_id")
        if self.stock_codes or self.max_stocks is not None or self.sampling_mode != "full":
            raise ValueError("strict US backtests prohibit custom codes and sampling")
        if self.refresh_data or self.refresh_sectors:
            raise ValueError("strict US backtests only use the immutable release")
        return self


class BacktestReplayRequest(BaseModel):
    source_backtest_id: str = Field(min_length=1, max_length=64)
    strategy_id: str | None = Field(default=None, max_length=64)
    start_date: str | None = None
    end_date: str | None = None
    execution_cost_multiplier: float = Field(default=1.0, ge=0.0, le=5.0)


class DecisionRequest(BaseModel):
    decision: Literal["APPROVED", "REJECTED"]
    note: str = Field(default="", max_length=500)
    push_tdx: bool = False
    reason_tags: list[str] = Field(default_factory=list, max_length=12)
    confidence: float | None = Field(default=None, ge=0, le=100)
    max_acceptable_loss: float | None = Field(default=None, ge=0, le=1)
    ai_review_id: str | None = Field(default=None, max_length=64)


class BriefRequest(BaseModel):
    run_id: str = Field(min_length=1, max_length=64)


class EarlyWinnerHistoryBuildRequest(BaseModel):
    start_year: int = Field(default=2018, ge=2000, le=2100)
    end_year: int = Field(default=2025, ge=2000, le=2100)


class ExperimentRequest(BaseModel):
    baseline_backtest_id: str = Field(min_length=1, max_length=64)
    hypothesis: str = Field(min_length=10, max_length=2000)


class StrategyGroupMemberRequest(BaseModel):
    strategy_id: str = Field(min_length=1, max_length=64)
    weight: float = Field(gt=0, le=1)
    role: Literal["alpha", "risk"] = "alpha"
    priority: int = Field(default=100, ge=0, le=10_000)


class StrategyGroupRequest(BaseModel):
    group_id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    version: str = Field(default="1.0.0", min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=80)
    description: str = Field(default="", max_length=500)
    composition_mode: Literal[
        "capital_sleeves", "score_fusion", "intersection", "risk_overlay"
    ] = "capital_sleeves"
    conflict_policy: Literal["risk_first", "net_score", "priority"] = "risk_first"
    enabled: bool = True
    members: list[StrategyGroupMemberRequest] = Field(min_length=1, max_length=20)


@lru_cache(maxsize=1)
def get_service() -> PlatformService:
    return PlatformService(PlatformConfig())


@lru_cache(maxsize=1)
def get_jobs() -> JobManager:
    return JobManager()


def create_app(config: PlatformConfig | None = None) -> FastAPI:
    if config is not None:
        get_service.cache_clear()
        service = PlatformService(config)
    else:
        service = get_service()
    jobs = JobManager(max_workers=service.config.performance.max_backtest_workers)
    backtests = BacktestService(service.config, service.database)
    data_cache = DataCacheManager(service.config, service.database)
    ai_research = AIResearchService(service.config, service.database)
    feedback = FeedbackService(service.config, service.database)
    strategy_lab = StrategyLabService(service.config, service.database)
    early_winner = service.early_winner
    early_winner_v2 = service.early_winner_v2
    early_winner_v3 = service.early_winner_v3
    early_winner_v4 = service.early_winner_v4
    early_winner_v5 = service.early_winner_v5
    early_winner_v6 = service.early_winner_v6
    early_winner_trading = service.early_winner_trading
    from .us_paper import USMomentumPaperService, USPaperConfig
    from .us_pit import USPITService
    from .us_program import USMomentumProgram
    from .us_tdx import check_tq_preflight

    us_pit = USPITService(service.config.us_pit_dir)
    us_paper = USMomentumPaperService(
        USPaperConfig(database_path=service.config.us_paper_database_path)
    )
    us_program = USMomentumProgram(service.config.us_program_database_path)
    app = FastAPI(title="通达信多策略投研平台", version="0.3.0", docs_url="/api/docs")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        return service.doctor()

    @app.get("/api/sources")
    def sources() -> list[dict[str, object]]:
        return service.data_hub.sources.as_records()

    @app.get("/api/data/us-pit/releases")
    def us_pit_releases() -> list[dict[str, Any]]:
        return us_pit.list_releases()

    @app.get("/api/data/us-pit/releases/{release_id}")
    def us_pit_release(release_id: str) -> dict[str, Any]:
        try:
            return us_pit.release_detail(release_id)
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="US PIT release not found") from exc

    @app.get("/api/data/us-pit/releases/{release_id}/quality")
    def us_pit_quality(release_id: str) -> dict[str, Any]:
        try:
            return us_pit.validate_release(release_id).to_dict()
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="US PIT release not found") from exc

    @app.get("/api/us-paper/status")
    def us_paper_status() -> dict[str, Any]:
        payload = us_paper.status()
        preflight = check_tq_preflight(portable_root=config.tdx_root).as_dict()
        program = us_program.status()
        payload["deployment_gate"] = preflight
        payload["broker_writes_enabled"] = False
        payload["program"] = program
        # Connectivity is only the prerequisite for the separate 20-session
        # quote qualification.  It never promotes an empty account by itself.
        payload["qualification"] = program["state"]
        payload["qualification_detail"] = (
            "The executor is admitted only after DATA_READY, BACKTEST_QUALIFIED "
            "and the 20-session TDX qualification."
            if preflight["ready"]
            else "TDX deployment preflight is not ready."
        )
        return payload

    @app.get("/api/strategies")
    def strategies() -> list[dict[str, Any]]:
        return _decode_rows(service.database.query("SELECT * FROM strategies ORDER BY strategy_id"))

    @app.get("/api/strategy-catalog")
    def strategy_catalog() -> dict[str, Any]:
        return service.strategy_catalog()

    @app.get("/api/frameworks")
    def frameworks() -> list[dict[str, Any]]:
        return _decode_rows(service.frameworks())

    @app.get("/api/frameworks/{framework_id}")
    def framework_detail(framework_id: str) -> dict[str, Any]:
        try:
            return _decode_row(service.framework_detail(framework_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Framework not found") from exc

    @app.post("/api/strategy-catalog/reload")
    def reload_strategy_catalog() -> dict[str, Any]:
        if jobs.active("scan") or jobs.active("backtest"):
            raise HTTPException(
                status_code=409,
                detail="Strategy plugins cannot be reloaded while a scan or backtest is running",
            )
        try:
            payload = service.reload_strategies()
            backtests.reload_strategies()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return payload

    @app.post("/api/strategy-groups")
    def save_strategy_group(request: StrategyGroupRequest) -> dict[str, object]:
        group = StrategyGroupDefinition(
            group_id=request.group_id,
            version=request.version,
            name=request.name,
            description=request.description,
            composition_mode=CompositionMode(request.composition_mode),
            conflict_policy=ConflictPolicy(request.conflict_policy),
            members=tuple(
                StrategyGroupMember(
                    item.strategy_id, item.weight, item.role, item.priority
                )
                for item in request.members
            ),
            enabled=request.enabled,
        )
        try:
            service.save_strategy_group(group)
            backtests.refresh_catalog()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return next(
            item for item in service.strategy_catalog()["groups"] if item["group_id"] == group.group_id
        )

    @app.delete("/api/strategy-groups/{group_id}", status_code=204)
    def delete_strategy_group(group_id: str) -> None:
        try:
            service.delete_strategy_group(group_id)
            backtests.refresh_catalog()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Strategy group not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/runs")
    def runs(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return _decode_rows(service.database.query("SELECT * FROM runs ORDER BY rowid DESC LIMIT ?", (limit,)))

    @app.post("/api/runs/scan", status_code=202)
    def scan(request: ScanRequest) -> dict[str, str]:
        active = jobs.active("scan")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        job_id = jobs.submit(
            "scan",
            service.run_scan,
            request.strategies,
            mode=request.mode,
            push_tdx=request.push_tdx,
            refresh_sectors=request.refresh_sectors,
            max_stocks=request.max_stocks,
            sampling_mode=request.sampling_mode,
            sample_seed=request.sample_seed,
            refresh_data=request.refresh_data,
        )
        return {"job_id": job_id, "status": "QUEUED"}

    @app.get("/api/jobs")
    def list_jobs() -> Any:
        return jsonable_encoder(jobs.list())

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str) -> Any:
        try:
            return jsonable_encoder(jobs.get(job_id))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Job not found") from exc

    @app.get("/api/cache")
    def cache_status() -> dict[str, Any]:
        return data_cache.status()

    @app.post("/api/cache/prune")
    def prune_cache() -> dict[str, Any]:
        return data_cache.prune()

    @app.get("/api/signals")
    def signals(
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        rows = _decode_rows(service.database.list_signals(limit=limit, status=status))
        if status == SignalStatus.PROPOSED.value:
            archived = {
                str(item["strategy_id"])
                for item in service.database.query(
                    "SELECT strategy_id FROM strategies WHERE archived=1"
                )
            }
            rows = [item for item in rows if str(item.get("strategy_id", "")) not in archived]
        return _attach_latest_reviews(service.database, rows)

    @app.post("/api/signals/{signal_id}/decision")
    def decide(signal_id: str, request: DecisionRequest) -> dict[str, Any]:
        try:
            return _decode_row(
                service.decide_signal(
                    signal_id,
                    approve=request.decision == SignalStatus.APPROVED.value,
                    note=request.note,
                    push_tdx=request.push_tdx,
                    reason_tags=request.reason_tags,
                    confidence=request.confidence,
                    max_acceptable_loss=request.max_acceptable_loss,
                    ai_review_id=request.ai_review_id,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Signal not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/order-groups")
    def order_groups(
        status: str | None = None,
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> list[dict[str, Any]]:
        return _decode_rows(service.database.list_order_groups(limit=limit, status=status))

    @app.post("/api/order-groups/{intent_id}/decision")
    def decide_order_group(intent_id: str, request: DecisionRequest) -> dict[str, Any]:
        try:
            return _decode_row(
                service.database.decide_order_group(
                    intent_id,
                    SignalStatus(request.decision),
                    request.note,
                )
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Order group not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/portfolio")
    def portfolio() -> dict[str, Any]:
        payload = service.database.portfolio_snapshot()
        payload["equity"] = service.database.query(
            "SELECT * FROM paper_equity ORDER BY timestamp DESC LIMIT 500"
        )
        return _decode_row(payload)

    @app.post("/api/backtests", status_code=202)
    def create_backtest(request: BacktestRequest) -> dict[str, str]:
        job_id = jobs.submit(
            "backtest",
            backtests.run,
            request.strategy_id,
            start_date=request.start_date,
            end_date=request.end_date,
            daily_bars=request.daily_bars,
            max_stocks=request.max_stocks,
            universe=request.universe,
            stock_codes=request.stock_codes,
            refresh_sectors=request.refresh_sectors,
            sampling_mode=request.sampling_mode,
            sample_seed=request.sample_seed,
            execution_cost_multiplier=request.execution_cost_multiplier,
            refresh_data=request.refresh_data,
            playbook_ids=request.playbook_ids,
            pit_release_id=request.pit_release_id,
        )
        return {"job_id": job_id, "status": "QUEUED"}

    @app.post("/api/backtests/replay", status_code=202)
    def replay_backtest(request: BacktestReplayRequest) -> dict[str, str]:
        job_id = jobs.submit(
            "backtest",
            backtests.replay_backtest,
            request.source_backtest_id,
            strategy_id=request.strategy_id,
            start_date=request.start_date,
            end_date=request.end_date,
            execution_cost_multiplier=request.execution_cost_multiplier,
        )
        return {"job_id": job_id, "status": "QUEUED"}

    @app.get("/api/backtests")
    def list_backtests(limit: int = Query(default=50, ge=1, le=500)) -> list[dict[str, Any]]:
        return _decode_rows(
            service.database.query("SELECT * FROM backtests ORDER BY started_at DESC LIMIT ?", (limit,))
        )

    @app.get("/api/backtests/{backtest_id}")
    def get_backtest(backtest_id: str) -> dict[str, Any]:
        rows = service.database.query("SELECT * FROM backtests WHERE backtest_id=?", (backtest_id,))
        if not rows:
            raise HTTPException(status_code=404, detail="Backtest not found")
        payload = _decode_row(rows[0])
        payload["equity"] = _decode_rows(service.database.query(
            "SELECT * FROM backtest_equity WHERE backtest_id=? ORDER BY timestamp", (backtest_id,)
        ))
        payload["trades"] = _decode_rows(service.database.query(
            "SELECT * FROM backtest_trades WHERE backtest_id=? ORDER BY timestamp", (backtest_id,)
        ))
        payload["position_changes"] = _position_changes(payload["trades"])
        payload["comparison"] = _strategy_comparison(payload)
        payload["states"] = _decode_rows(
            service.database.query(
                "SELECT * FROM backtest_states WHERE backtest_id=? ORDER BY timestamp, strategy_id",
                (backtest_id,),
            )
        )
        payload["playbook_states"] = _decode_rows(
            service.database.query(
                """SELECT * FROM backtest_playbook_states
                WHERE backtest_id=? ORDER BY timestamp, playbook_id""",
                (backtest_id,),
            )
        )
        metrics = payload.get("metrics") or {}
        payload["playbook_attribution"] = (
            metrics.get("components", {})
            .get("course49_system", {})
            .get("playbook_attribution", [])
            if isinstance(metrics, dict)
            else []
        )
        return payload

    @app.get("/api/symbols/{code}/analysis")
    def symbol_analysis(code: str) -> dict[str, Any]:
        code = code.upper()
        return _decode_row(
            {
                "code": code,
                "signals": service.database.query(
                    "SELECT * FROM signals WHERE code=? ORDER BY generated_at DESC LIMIT 100", (code,)
                ),
                "positions": service.database.query("SELECT * FROM paper_positions WHERE code=?", (code,)),
                "fills": service.database.query(
                    "SELECT * FROM paper_fills WHERE code=? ORDER BY timestamp DESC LIMIT 100", (code,)
                ),
            }
        )

    @app.get("/api/dashboard")
    def dashboard() -> dict[str, Any]:
        latest_run = service.database.query("SELECT * FROM runs ORDER BY rowid DESC LIMIT 1")
        pending = service.database.query("SELECT COUNT(*) AS count FROM signals WHERE status='PROPOSED'")[0]["count"]
        accounts = service.database.query("SELECT * FROM paper_accounts ORDER BY strategy_id")
        positions = service.database.query("SELECT * FROM paper_positions ORDER BY strategy_id, code")
        group_positions = service.database.group_positions()
        backtest = service.database.query("SELECT * FROM backtests ORDER BY started_at DESC LIMIT 1")
        return _decode_row(
            {
                "doctor": service.doctor(),
                "latest_run": latest_run[0] if latest_run else None,
                "pending_signals": pending,
                "accounts": accounts,
                "positions": positions,
                "group_positions": group_positions,
                "strategy_catalog": service.strategy_catalog(),
                "latest_backtest": backtest[0] if backtest else None,
            }
        )

    @app.post("/api/research/daily", status_code=202)
    def daily_research(request: ScanRequest) -> dict[str, str]:
        active = jobs.active("daily_research")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        return {
            "job_id": jobs.submit(
                "daily_research",
                service.run_daily_research,
                request.strategies,
                refresh_sectors=request.refresh_sectors,
                max_stocks=request.max_stocks,
                sampling_mode=request.sampling_mode,
                sample_seed=request.sample_seed,
                refresh_data=request.refresh_data,
            ),
            "status": "QUEUED",
        }

    @app.get("/api/research/early-winner")
    def early_winner_detail() -> dict[str, Any]:
        return early_winner.detail()

    @app.get("/api/research/early-winner-v2")
    def early_winner_v2_detail() -> dict[str, Any]:
        return early_winner_v2.detail()

    @app.post("/api/research/early-winner-v2/development-audit", status_code=202)
    def audit_early_winner_v2() -> dict[str, str]:
        active = jobs.active("early_winner_v2_project")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}

        def run_action(progress_callback: Any | None = None) -> dict[str, Any]:
            try:
                return early_winner_v2.run_development_audit(
                    progress_callback=progress_callback
                )
            except ResearchDataBlockedError as exc:
                service.database.update_research_project(
                    EARLY_WINNER_V2_PROJECT_ID,
                    status="BLOCKED_DATA",
                )
                return {
                    "project_id": EARLY_WINNER_V2_PROJECT_ID,
                    "status": "BLOCKED_DATA",
                    "error": str(exc),
                    "trade_signals": 0,
                }

        return {
            "job_id": jobs.submit("early_winner_v2_project", run_action),
            "status": "QUEUED",
        }

    @app.get("/api/research/early-winner-v3")
    def early_winner_v3_detail() -> dict[str, Any]:
        return early_winner_v3.detail()

    def submit_early_winner_v3(action: str) -> dict[str, str]:
        active = jobs.active("early_winner_v3_project")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}

        def run_action(progress_callback: Any | None = None) -> dict[str, Any]:
            try:
                if action == "supplement":
                    return early_winner_v3.build_supplemental_snapshot(
                        progress_callback=progress_callback
                    )
                if action == "development-audit":
                    return early_winner_v3.run_development_audit(
                        progress_callback=progress_callback
                    )
                raise ValueError(action)
            except ResearchDataBlockedError as exc:
                service.database.update_research_project(
                    EARLY_WINNER_V3_PROJECT_ID, status="BLOCKED_DATA"
                )
                return {
                    "project_id": EARLY_WINNER_V3_PROJECT_ID,
                    "status": "BLOCKED_DATA",
                    "error": str(exc),
                    "trade_signals": 0,
                }

        return {
            "job_id": jobs.submit("early_winner_v3_project", run_action),
            "status": "QUEUED",
        }

    @app.post("/api/research/early-winner-v3/supplement", status_code=202)
    def supplement_early_winner_v3() -> dict[str, str]:
        return submit_early_winner_v3("supplement")

    @app.post("/api/research/early-winner-v3/development-audit", status_code=202)
    def audit_early_winner_v3() -> dict[str, str]:
        return submit_early_winner_v3("development-audit")

    @app.get("/api/research/early-winner-v4")
    def early_winner_v4_detail() -> dict[str, Any]:
        return early_winner_v4.detail()

    @app.get("/api/research/early-winner-v5")
    def early_winner_v5_detail() -> dict[str, Any]:
        """Read-only V5 preregistration. Frozen validation has no API action."""

        return early_winner_v5.detail()

    @app.get("/api/research/early-winner-v6")
    def early_winner_v6_detail() -> dict[str, Any]:
        """Read-only V6 protocol and frozen-ledger status."""

        return early_winner_v6.detail()

    def submit_early_winner_v4(action: str) -> dict[str, str]:
        active = jobs.active("early_winner_v4_project")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}

        def run_action(progress_callback: Any | None = None) -> dict[str, Any]:
            try:
                if action == "build-labels":
                    return early_winner_v4.build_label_snapshot(
                        progress_callback=progress_callback
                    )
                if action == "development-audit":
                    return early_winner_v4.run_development_audit(
                        progress_callback=progress_callback
                    )
                raise ValueError(action)
            except Exception:
                service.database.update_research_project(
                    EARLY_WINNER_V4_PROJECT_ID, status="BLOCKED_DATA"
                )
                raise

        return {
            "job_id": jobs.submit("early_winner_v4_project", run_action),
            "status": "QUEUED",
        }

    @app.post("/api/research/early-winner-v4/build-labels", status_code=202)
    def build_early_winner_v4_labels() -> dict[str, str]:
        return submit_early_winner_v4("build-labels")

    @app.post("/api/research/early-winner-v4/development-audit", status_code=202)
    def audit_early_winner_v4() -> dict[str, str]:
        return submit_early_winner_v4("development-audit")

    @app.get("/api/research/early-winner/candidates")
    def early_winner_candidates(
        method: Literal["rule", "ml"] | None = None,
        asof: str | None = None,
    ) -> list[dict[str, Any]]:
        try:
            return early_winner.candidates(method=method, asof=asof)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/research/early-winner/refresh", status_code=202)
    def refresh_early_winner() -> dict[str, str]:
        raise HTTPException(
            status_code=410,
            detail="early_winner_v1 is audit-only; use a newly sealed research version",
        )

    @app.post("/api/research/early-winner/train", status_code=202)
    def train_early_winner() -> dict[str, str]:
        raise HTTPException(
            status_code=410,
            detail="early_winner_v1 is audit-only; use a newly sealed research version",
        )

    @app.post("/api/research/early-winner/validate", status_code=202)
    def validate_early_winner() -> dict[str, str]:
        raise HTTPException(
            status_code=410,
            detail="early_winner_v1 is audit-only; use a newly sealed research version",
        )

    @app.get("/api/research/early-winner/history/status")
    def early_winner_history_status() -> dict[str, Any]:
        return early_winner.history_status()

    @app.post("/api/research/early-winner/history/build", status_code=202)
    def build_early_winner_history(request: EarlyWinnerHistoryBuildRequest) -> dict[str, str]:
        if request.end_year < request.start_year:
            raise HTTPException(status_code=422, detail="end_year must not precede start_year")
        raise HTTPException(
            status_code=410,
            detail="early_winner_v1 history builder is retired after the survivorship-bias audit",
        )

    # This application profile is research + paper-only.  Legacy real-trading
    # routes are deliberately not mounted; callers receive 404 instead of a
    # feature that could be enabled by configuration or an operator token.

    @app.post("/api/research/briefs", status_code=202)
    def generate_brief(request: BriefRequest) -> dict[str, str]:
        active = jobs.active("research_brief")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        if not service.database.query("SELECT run_id FROM runs WHERE run_id=?", (request.run_id,)):
            raise HTTPException(status_code=404, detail="Run not found")
        return {
            "job_id": jobs.submit("research_brief", ai_research.generate_brief, request.run_id),
            "status": "QUEUED",
        }

    @app.get("/api/research/briefs")
    def list_briefs(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        return ai_research.list_briefs(limit)

    @app.get("/api/research/briefs/{brief_id}")
    def get_brief(brief_id: str) -> dict[str, Any]:
        try:
            return ai_research.get_brief(brief_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Research brief not found") from exc

    @app.post("/api/research/feedback/refresh", status_code=202)
    def refresh_feedback() -> dict[str, str]:
        active = jobs.active("feedback")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        return {"job_id": jobs.submit("feedback", feedback.refresh), "status": "QUEUED"}

    @app.get("/api/research/feedback/summary")
    def feedback_summary() -> dict[str, Any]:
        return feedback.summary()

    @app.post("/api/research/weekly-triangle/observations/refresh", status_code=202)
    def refresh_weekly_triangle_observations() -> dict[str, str]:
        active = jobs.active("weekly_triangle_observations")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        return {
            "job_id": jobs.submit(
                "weekly_triangle_observations",
                service.weekly_triangle_observations.refresh,
            ),
            "status": "QUEUED",
        }

    @app.get("/api/research/weekly-triangle/observations")
    def weekly_triangle_observations(
        limit: int = Query(default=200, ge=1, le=1000),
    ) -> dict[str, Any]:
        return service.weekly_triangle_observations.summary(limit)

    @app.get("/api/research/weekly-triangle/setup-stability")
    def weekly_triangle_setup_stability() -> dict[str, Any]:
        path = (
            service.config.repository_root
            / "data"
            / "research"
            / "weekly_triangle_v1"
            / "setup_stability.json"
        )
        if not path.exists():
            return {"status": "NOT_AVAILABLE", "path": str(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Weekly triangle setup study is unreadable: {exc}",
            ) from exc
        payload["status"] = (
            "PROMOTION_QUALIFIED"
            if payload.get("promotion_qualified")
            else "RESEARCH_ONLY"
        )
        payload["path"] = str(path)
        return payload

    @app.get("/api/research/pairs-arbitrage/validation")
    def pairs_arbitrage_validation() -> dict[str, Any]:
        path = (
            service.config.repository_root
            / "data"
            / "research"
            / "pairs_arbitrage_v1"
            / "historical_validation.json"
        )
        if not path.exists():
            return {"status": "NOT_AVAILABLE", "path": str(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Pairs-arbitrage validation is unreadable: {exc}",
            ) from exc
        payload["status"] = str(payload.get("decision") or "UNKNOWN")
        payload["path"] = str(path)
        return payload

    @app.get("/api/research/chan/validation")
    def chan_validation() -> dict[str, Any]:
        path = (
            service.config.repository_root
            / "data"
            / "research"
            / "chan_v1"
            / "historical_validation.json"
        )
        if not path.exists():
            return {"status": "NOT_AVAILABLE", "path": str(path)}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"Chan validation is unreadable: {exc}",
            ) from exc
        payload["status"] = str(payload.get("decision") or "UNKNOWN")
        payload["path"] = str(path)
        return payload

    @app.post("/api/research/experiments", status_code=202)
    def create_experiment(request: ExperimentRequest) -> dict[str, str]:
        active = jobs.active("strategy_experiment")
        if active:
            return {"job_id": str(active["job_id"]), "status": str(active["status"])}
        return {
            "job_id": jobs.submit(
                "strategy_experiment",
                strategy_lab.create_experiment,
                request.baseline_backtest_id,
                request.hypothesis,
            ),
            "status": "QUEUED",
        }

    @app.get("/api/research/experiments")
    def list_experiments(limit: int = Query(default=50, ge=1, le=200)) -> list[dict[str, Any]]:
        return strategy_lab.list_experiments(limit)

    @app.get("/api/research/experiments/{experiment_id}")
    def get_experiment(experiment_id: str) -> dict[str, Any]:
        try:
            return strategy_lab.get_experiment(experiment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Strategy experiment not found") from exc

    @app.post("/api/research/experiments/{experiment_id}/promote")
    def promote_experiment(experiment_id: str) -> dict[str, Any]:
        try:
            return strategy_lab.promote(experiment_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Strategy experiment not found") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    frontend_dist = service.config.frontend_dist
    if frontend_dist.exists() and (frontend_dist / "index.html").exists():
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{frontend_path:path}", include_in_schema=False)
        def frontend(frontend_path: str) -> FileResponse:
            if frontend_path.startswith("api/"):
                raise HTTPException(status_code=404, detail="API route not found")
            candidate = (frontend_dist / frontend_path).resolve()
            if candidate.is_relative_to(frontend_dist.resolve()) and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")
    return app


def _position_changes(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    holdings: dict[tuple[str, str, str], int] = {}
    changes: list[dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: str(item.get("timestamp", ""))):
        strategy_id = str(trade.get("strategy_id", ""))
        code = str(trade.get("code", ""))
        group_key = str(trade.get("group_key", ""))
        key = (strategy_id, group_key, code)
        before = holdings.get(key, 0)
        quantity = int(trade.get("quantity", 0) or 0)
        side = str(trade.get("side", "")).upper()
        signed_quantity = quantity if side in {"BUY", "COVER"} else -quantity
        after = before + signed_quantity
        holdings[key] = after
        changes.append(
            {
                "timestamp": trade.get("timestamp"),
                "strategy_id": strategy_id,
                "code": code,
                "group_key": group_key,
                "side": trade.get("side"),
                "quantity_change": signed_quantity,
                "quantity_before": before,
                "quantity_after": after,
                "price": float(trade.get("price", 0.0) or 0.0),
                "trade_value": quantity * float(trade.get("price", 0.0) or 0.0),
            }
        )
    return changes


def _strategy_comparison(payload: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = payload.get("metrics") or {}
    if not isinstance(metrics, dict):
        return []
    components = metrics.get("components")
    if not isinstance(components, dict) or not components:
        components = {str(payload.get("strategy_id", "strategy")): metrics}
    rows = []
    for strategy_id, values in components.items():
        if not isinstance(values, dict):
            continue
        rows.append({"strategy_id": strategy_id, **values})
    return rows


def _decode_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_decode_row(row) for row in rows]


def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    json_keys = {
        "metadata_json",
        "parameters_json",
        "strategies_json",
        "metrics_json",
        "reason_codes",
        "evidence",
        "state_json",
        "data_requirements_json",
        "asset_classes",
        "content_json",
        "input_json",
        "usage_json",
        "supporting_json",
        "opposing_json",
        "missing_json",
        "evidence_refs_json",
        "validation_json",
        "baseline_metrics_json",
        "candidate_metrics_json",
        "stress_metrics_json",
        "reason_tags",
        "blocked_reasons",
        "funnel_json",
    }
    for key, value in row.items():
        if isinstance(value, list):
            result[key] = [_decode_row(item) if isinstance(item, dict) else item for item in value]
        elif isinstance(value, dict):
            result[key] = _decode_row(value)
        elif key in json_keys and isinstance(value, str):
            try:
                result[key.removesuffix("_json")] = json.loads(value)
            except json.JSONDecodeError:
                result[key.removesuffix("_json")] = value
        else:
            result[key] = value
    return result


def _attach_latest_reviews(database: Any, signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not signals:
        return signals
    reviews = database.query(
        """SELECT r.*, b.created_at FROM ai_signal_reviews r
        JOIN research_briefs b ON b.brief_id=r.brief_id
        WHERE b.status='SUCCEEDED' ORDER BY b.created_at DESC"""
    )
    latest: dict[str, dict[str, Any]] = {}
    for review in _decode_rows(reviews):
        latest.setdefault(str(review["signal_id"]), review)
    for signal in signals:
        signal["ai_review"] = latest.get(str(signal["signal_id"]))
    return signals
