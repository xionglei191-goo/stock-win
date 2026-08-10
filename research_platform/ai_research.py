from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from .config import PlatformConfig
from .storage import Database


BRIEF_PROMPT_VERSION = "daily-brief-v1"


class EvidenceClaim(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)
    confidence: float = Field(ge=0, le=1)
    limitations: list[str] = Field(default_factory=list, max_length=8)


class SignalReviewOutput(BaseModel):
    signal_id: str
    recommendation: Literal["SUPPORT", "OPPOSE", "INSUFFICIENT"]
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=500)
    supporting: list[str] = Field(default_factory=list, max_length=8)
    opposing: list[str] = Field(default_factory=list, max_length=8)
    missing: list[str] = Field(default_factory=list, max_length=8)
    evidence_refs: list[str] = Field(min_length=1, max_length=12)


class DailyBriefOutput(BaseModel):
    headline: str = Field(min_length=1, max_length=160)
    market_summary: EvidenceClaim
    strategy_summaries: list[EvidenceClaim] = Field(default_factory=list, max_length=12)
    portfolio_risks: list[EvidenceClaim] = Field(default_factory=list, max_length=12)
    data_gaps: list[EvidenceClaim] = Field(default_factory=list, max_length=12)
    caveats: list[str] = Field(default_factory=list, max_length=12)
    signal_reviews: list[SignalReviewOutput] = Field(default_factory=list, max_length=200)


class AIResearchService:
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

    def generate_brief(self, run_id: str) -> dict[str, Any]:
        context, evidence_ids, signal_ids = self.build_context(run_id)
        brief_id = uuid4().hex
        now = datetime.now().astimezone().isoformat()
        payload = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        input_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        run = self.database.query("SELECT snapshot_id FROM runs WHERE run_id=?", (run_id,))[0]
        self.database.execute(
            """INSERT INTO research_briefs
            (brief_id, run_id, status, created_at, prompt_version, input_hash, input_json, snapshot_id)
            VALUES (?, ?, 'RUNNING', ?, ?, ?, ?, ?)""",
            (brief_id, run_id, now, BRIEF_PROMPT_VERSION, input_hash, payload, run.get("snapshot_id")),
        )
        try:
            if not self.config.openai_api_key and self._provided_client is None:
                raise RuntimeError("OPENAI_API_KEY is not configured")
            client = self._provided_client or self._create_client()
            response = None
            output = None
            validation_error = ""
            for attempt in range(2):
                correction = (
                    f"\n上次输出未通过本地校验：{validation_error}。请仅使用输入中的 evidence_id 并完整重试。"
                    if attempt else ""
                )
                response = client.responses.parse(
                    model=self.config.openai_model,
                    store=False,
                    reasoning={"effort": "low"},
                    input=[
                        {
                            "role": "system",
                            "content": (
                                "你是本地量化研究平台的证据摘要助手。只解释输入 JSON 中的事实，"
                                "不得计算新指标、承诺收益或代替人工审批。每条结论必须引用给定 evidence_id。"
                                "必须为输入中的每个 signal 生成且只生成一条 signal_review。"
                                + correction
                            ),
                        },
                        {"role": "user", "content": payload},
                    ],
                    text_format=DailyBriefOutput,
                )
                output = response.output_parsed
                try:
                    if output is None:
                        raise ValueError("Model returned no structured output")
                    self._validate_output(output, evidence_ids, signal_ids)
                    break
                except ValueError as exc:
                    validation_error = str(exc)
                    if attempt == 1:
                        raise
            assert response is not None and output is not None
            content = output.model_dump(mode="json")
            usage = self._to_json(getattr(response, "usage", None))
            generated_at = datetime.now().astimezone().isoformat()
            with self.database.connect() as connection:
                connection.execute(
                    """UPDATE research_briefs SET status='SUCCEEDED', generated_at=?, model=?,
                    response_id=?, content_json=?, usage_json=?, error='' WHERE brief_id=?""",
                    (
                        generated_at,
                        str(getattr(response, "model", self.config.openai_model)),
                        str(getattr(response, "id", "")),
                        json.dumps(content, ensure_ascii=False),
                        json.dumps(usage, ensure_ascii=False),
                        brief_id,
                    ),
                )
                for review in output.signal_reviews:
                    connection.execute(
                        """INSERT INTO ai_signal_reviews
                        (review_id, brief_id, signal_id, recommendation, confidence, summary,
                         supporting_json, opposing_json, missing_json, evidence_refs_json)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            uuid4().hex,
                            brief_id,
                            review.signal_id,
                            review.recommendation,
                            review.confidence,
                            review.summary,
                            json.dumps(review.supporting, ensure_ascii=False),
                            json.dumps(review.opposing, ensure_ascii=False),
                            json.dumps(review.missing, ensure_ascii=False),
                            json.dumps(review.evidence_refs, ensure_ascii=False),
                        ),
                    )
        except Exception as exc:
            self.database.execute(
                "UPDATE research_briefs SET status='FAILED', generated_at=?, model=?, error=? WHERE brief_id=?",
                (datetime.now().astimezone().isoformat(), self.config.openai_model, str(exc), brief_id),
            )
        return self.get_brief(brief_id)

    def build_context(self, run_id: str) -> tuple[dict[str, Any], set[str], set[str]]:
        rows = self.database.query("SELECT * FROM runs WHERE run_id=?", (run_id,))
        if not rows:
            raise KeyError(run_id)
        run = self._decode_row(rows[0])
        signals = [self._decode_row(item) for item in self.database.query(
            "SELECT * FROM signals WHERE run_id=? ORDER BY strategy_id, code LIMIT 200", (run_id,)
        )]
        positions = [self._decode_row(item) for item in self.database.query(
            "SELECT * FROM paper_positions ORDER BY strategy_id, code"
        )]
        backtests = [self._decode_row(item) for item in self.database.query(
            "SELECT * FROM backtests WHERE status='SUCCEEDED' ORDER BY finished_at DESC LIMIT 3"
        )]
        data_health = [self._decode_row(item) for item in self.database.query(
            """SELECT ds.* FROM data_snapshots ds
            INNER JOIN (
                SELECT dataset, MAX(created_at) AS latest_at
                FROM data_snapshots GROUP BY dataset
            ) latest ON latest.dataset=ds.dataset AND latest.latest_at=ds.created_at
            ORDER BY ds.dataset LIMIT 12"""
        )]
        evidence_ids = {f"run:{run_id}"}
        signal_ids = {str(item["signal_id"]) for item in signals}
        evidence_ids.update(f"signal:{item['signal_id']}" for item in signals)
        evidence_ids.update(f"position:{item['strategy_id']}:{item['code']}" for item in positions)
        evidence_ids.update(f"backtest:{item['backtest_id']}" for item in backtests)
        evidence_ids.update(f"data_health:{item['dataset']}:{item['snapshot_id']}" for item in data_health)
        context = {
            "as_of": datetime.now().astimezone().isoformat(),
            "run": {"evidence_id": f"run:{run_id}", **self._select_run(run)},
            "signals": [
                {"evidence_id": f"signal:{item['signal_id']}", **self._select_signal(item)}
                for item in signals
            ],
            "positions": [
                {
                    "evidence_id": f"position:{item['strategy_id']}:{item['code']}",
                    **self._select_position(item),
                }
                for item in positions
            ],
            "recent_backtests": [
                {"evidence_id": f"backtest:{item['backtest_id']}", **self._select_backtest(item)}
                for item in backtests
            ],
            "data_health": [
                {
                    "evidence_id": f"data_health:{item['dataset']}:{item['snapshot_id']}",
                    **self._select_data_health(item),
                }
                for item in data_health
            ],
        }
        return context, evidence_ids, signal_ids

    def list_briefs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._decode_row(item) for item in self.database.query(
            "SELECT * FROM research_briefs ORDER BY created_at DESC LIMIT ?", (limit,)
        )]

    def get_brief(self, brief_id: str) -> dict[str, Any]:
        rows = self.database.query("SELECT * FROM research_briefs WHERE brief_id=?", (brief_id,))
        if not rows:
            raise KeyError(brief_id)
        result = self._decode_row(rows[0])
        result["reviews"] = [self._decode_row(item) for item in self.database.query(
            "SELECT * FROM ai_signal_reviews WHERE brief_id=? ORDER BY signal_id", (brief_id,)
        )]
        return result

    def _create_client(self) -> Any:
        from openai import OpenAI

        return OpenAI(
            api_key=self.config.openai_api_key,
            timeout=self.config.openai_timeout_seconds,
            max_retries=self.config.openai_max_retries,
        )

    @staticmethod
    def _validate_output(
        output: DailyBriefOutput, evidence_ids: set[str], signal_ids: set[str]
    ) -> None:
        refs: list[str] = []
        claims = [output.market_summary, *output.strategy_summaries, *output.portfolio_risks, *output.data_gaps]
        for claim in claims:
            refs.extend(claim.evidence_refs)
        review_ids = {review.signal_id for review in output.signal_reviews}
        if review_ids != signal_ids or len(output.signal_reviews) != len(signal_ids):
            raise ValueError("Signal reviews do not exactly match the run signals")
        for review in output.signal_reviews:
            refs.extend(review.evidence_refs)
            if f"signal:{review.signal_id}" not in review.evidence_refs:
                raise ValueError("Each signal review must cite its signal")
        unknown = sorted(set(refs) - evidence_ids)
        if unknown:
            raise ValueError(f"Unknown evidence references: {', '.join(unknown[:5])}")

    @staticmethod
    def _decode_row(row: dict[str, Any]) -> dict[str, Any]:
        result = dict(row)
        for key in list(result):
            if key.endswith("_json") or key in {
                "metadata_json", "metrics_json", "parameters_json", "reason_codes", "evidence",
                "supporting_json", "opposing_json", "missing_json", "evidence_refs_json",
            }:
                value = result[key]
                try:
                    result[key.removesuffix("_json")] = json.loads(value) if isinstance(value, str) else value
                except json.JSONDecodeError:
                    result[key.removesuffix("_json")] = value
        return result

    @staticmethod
    def _select_run(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in ("run_id", "status", "mode", "strategies", "snapshot_id", "metadata")}

    @staticmethod
    def _select_signal(row: dict[str, Any]) -> dict[str, Any]:
        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
        allowed = {
            key: evidence.get(key) for key in (
                "market_phase", "market_score", "market_style", "style_suitability", "trade_mode",
                "sector_code", "sector_name", "theme_phase", "role", "limit_streak",
                "board_quality_score", "lhb", "limit_behavior",
            ) if key in evidence
        }
        return {
            key: row.get(key) for key in (
                "signal_id", "strategy_id", "strategy_version", "generated_at", "available_at",
                "code", "side", "strength", "target_weight", "horizon", "valid_until",
                "stop_price", "status", "reason_codes",
            )
        } | {"evidence": allowed}

    @staticmethod
    def _select_position(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in (
            "strategy_id", "code", "quantity", "average_price", "entry_time", "stop_price", "last_price"
        )}

    @staticmethod
    def _select_backtest(row: dict[str, Any]) -> dict[str, Any]:
        metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
        return {
            "backtest_id": row.get("backtest_id"),
            "strategy_id": row.get("strategy_id"),
            "start_date": row.get("start_date"),
            "end_date": row.get("end_date"),
            "snapshot_id": row.get("snapshot_id"),
            "metrics": {key: metrics.get(key) for key in (
                "total_return", "annualized_return", "max_drawdown", "sharpe_ratio", "closed_trades",
                "win_rate", "profit_factor", "average_capital_invested", "validation",
            ) if key in metrics},
        }

    @staticmethod
    def _select_data_health(row: dict[str, Any]) -> dict[str, Any]:
        return {key: row.get(key) for key in (
            "dataset", "snapshot_id", "created_at", "row_count", "content_hash", "source"
        )}

    @staticmethod
    def _to_json(value: Any) -> Any:
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            return value.model_dump(mode="json")
        return value if isinstance(value, (dict, list, str, int, float, bool)) else str(value)
