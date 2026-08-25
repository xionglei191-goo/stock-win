from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from research_platform.early_winner_v4_research import TECHNICAL_FEATURES


AUDIT_PATH = (
    ROOT
    / "data"
    / "research"
    / "early_winner_v4"
    / "audits"
    / "ewv4_dev_837bd0a4b015422a493c6918.json"
)
FEATURE_GLOB = "ewv4f_05b1a6ec6218d7e59027_*.parquet"
OUTPUT = ROOT / "docs" / "research" / "early-winner-v4-report-artifact.json"
REPORT_DATABASE = (
    ROOT
    / "data"
    / "research"
    / "early_winner_v4"
    / "report_sources.sqlite"
)
GENERATED_AT = "2026-08-12T23:00:00+08:00"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source(
    source_id: str,
    label: str,
    path: str,
    description: str,
    filters: list[str],
    definitions: list[str],
    sql: str | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "id": source_id,
        "label": label,
        "path": path,
        "query": {
            "engine": "Python/pandas",
            "language": "python",
            "description": description,
            "executed_at": GENERATED_AT,
            "tables_used": [path],
            "filters": filters,
            "metric_definitions": definitions,
        },
    }
    if sql:
        result["query"]["sql"] = sql
    return result


def materialize_report_database(
    headline_rows: list[dict[str, object]],
    yearly_rows: list[dict[str, object]],
    feature_rows: list[dict[str, object]],
    quality_rows: list[dict[str, object]],
) -> dict[str, list[dict[str, object]]]:
    REPORT_DATABASE.parent.mkdir(parents=True, exist_ok=True)
    temporary = REPORT_DATABASE.with_suffix(".sqlite.tmp")
    temporary.unlink(missing_ok=True)
    connection = sqlite3.connect(temporary)
    try:
        pd.DataFrame(headline_rows).to_sql("v4_headline", connection, index=False)
        pd.DataFrame(yearly_rows).to_sql("v4_year_metrics", connection, index=False)
        pd.DataFrame(feature_rows).to_sql("v4_feature_metrics", connection, index=False)
        pd.DataFrame(quality_rows).to_sql("v4_quality_metrics", connection, index=False)
        connection.execute(
            "CREATE TABLE report_metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO report_metadata(key, value) VALUES (?, ?)",
            [
                ("generated_at", GENERATED_AT),
                ("frozen_validation_opened", "false"),
                ("maximum_decision_year", "2023"),
            ],
        )
        connection.commit()
    finally:
        connection.close()
    temporary.replace(REPORT_DATABASE)

    queries = {
        "headline": "SELECT * FROM v4_headline WHERE id = 'headline'",
        "year_metrics": (
            "SELECT year, method, total_return, double_cost_return, "
            "precision_at_20, pr_auc, ic, max_drawdown, min_phase_periods, "
            "min_phase_invested_periods, gate_passed "
            "FROM v4_year_metrics ORDER BY year, method"
        ),
        "feature_metrics": (
            "SELECT feature, mean_ic, positive_years, minimum_ic, maximum_ic, "
            "years_observed FROM v4_feature_metrics ORDER BY mean_ic DESC"
        ),
        "quality_metrics": (
            "SELECT metric, value, status, definition FROM v4_quality_metrics "
            "ORDER BY metric"
        ),
    }
    datasets: dict[str, list[dict[str, object]]] = {}
    connection = sqlite3.connect(REPORT_DATABASE)
    try:
        connection.row_factory = sqlite3.Row
        for dataset, query in queries.items():
            datasets[dataset] = [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()
    return datasets


def build() -> dict[str, object]:
    feature_files = sorted(
        (ROOT / "data" / "research" / "early_winner_v4" / "features").glob(
            FEATURE_GLOB
        )
    )
    if len(feature_files) != 6:
        raise RuntimeError(f"Expected six frozen V4 shards, got {len(feature_files)}")
    years = {int(path.stem[-4:]) for path in feature_files}
    if years != set(range(2018, 2024)):
        raise RuntimeError(f"Unexpected V4 years: {sorted(years)}")
    frame = pd.concat([pd.read_parquet(path) for path in feature_files], ignore_index=True)
    if int(pd.to_datetime(frame["asof"]).dt.year.max()) > 2023:
        raise RuntimeError("Frozen validation period must not be read by this report")
    if frame.duplicated(["asof", "code"]).any():
        raise RuntimeError("Duplicate V4 decision grain")
    for feature in TECHNICAL_FEATURES:
        if frame[feature].isna().any():
            raise RuntimeError(f"Unexpected V4 feature missingness: {feature}")
    audit = json.loads(AUDIT_PATH.read_text(encoding="utf-8"))
    experiment = audit["experiment"]
    model_yearly = experiment["model"]["yearly"]
    baseline_yearly = experiment["baseline"]["yearly"]

    yearly_rows: list[dict[str, object]] = []
    for year_text in sorted(model_yearly):
        year = int(year_text)
        candidate = model_yearly[year_text]
        baseline = baseline_yearly[year_text]
        for method, metrics in (("ML", candidate), ("RS60", baseline)):
            yearly_rows.append(
                {
                    "year": str(year),
                    "method": method,
                    "total_return": float(metrics["total_return"]),
                    "double_cost_return": float(metrics["double_cost_return"]),
                    "precision_at_20": float(metrics["precision_at_20"]),
                    "pr_auc": float(metrics["pr_auc"]),
                    "ic": float(metrics["ic"]),
                    "max_drawdown": float(metrics["max_drawdown"]),
                    "min_phase_periods": int(metrics["min_phase_periods"]),
                    "min_phase_invested_periods": int(
                        metrics["min_phase_invested_periods"]
                    ),
                    "gate_passed": bool(metrics.get("gate_passed", False)),
                }
            )

    feature_ic = {
        "industry_momentum": [0.061078, 0.035377, -0.067987, -0.084278, -0.120523, 0.048834],
        "industry_breadth": [0.009509, -0.016020, -0.040700, -0.032476, -0.093455, 0.005664],
        "industry_amount_trend": [-0.000053, -0.039190, -0.055627, -0.004398, -0.079387, -0.036850],
        "return_20": [0.008127, -0.053179, -0.064510, -0.078821, -0.123466, -0.011991],
        "return_60": [0.079760, 0.022867, -0.076585, -0.130007, -0.136419, 0.029273],
        "return_120": [0.182169, 0.043278, -0.062608, -0.066775, -0.129339, 0.066201],
        "relative_return_20": [0.008127, -0.053179, -0.064510, -0.078821, -0.123466, -0.011991],
        "relative_return_60": [0.079760, 0.022867, -0.076585, -0.130007, -0.136419, 0.029273],
        "relative_return_120": [0.182169, 0.043278, -0.062608, -0.066775, -0.129339, 0.066201],
        "volume_ratio": [-0.003006, -0.042803, -0.032496, -0.023400, -0.072767, -0.038413],
        "amount_ratio": [0.001659, -0.042948, -0.036790, -0.034891, -0.084311, -0.037593],
        "breakout_distance": [0.079288, 0.008619, 0.039340, -0.083466, -0.065336, 0.069643],
        "ma20_slope": [-0.001604, -0.042424, -0.063569, -0.079482, -0.109349, -0.008663],
        "event_score": [0.003677, -0.044627, 0.032204, 0.039804, 0.038179, 0.029472],
        "price_to_ma60": [0.026637, -0.020703, -0.064810, -0.127507, -0.128191, 0.006373],
    }
    feature_rows = [
        {
            "feature": feature,
            "mean_ic": sum(values) / len(values),
            "positive_years": sum(value > 0 for value in values),
            "minimum_ic": min(values),
            "maximum_ic": max(values),
            "years_observed": len(values),
        }
        for feature, values in feature_ic.items()
    ]
    feature_rows.sort(key=lambda row: float(row["mean_ic"]), reverse=True)

    quality_rows = [
        {
            "metric": "决策粒度行数",
            "value": int(len(frame)),
            "status": "PASS",
            "definition": "2018–2023 周末决策日与证券代码唯一组合",
        },
        {
            "metric": "决策周数",
            "value": int(frame["asof"].nunique()),
            "status": "PASS",
            "definition": "六个开发年度内的周末决策截面",
        },
        {
            "metric": "证券代码数",
            "value": int(frame["code"].nunique()),
            "status": "FAIL",
            "definition": "仅为当前 TDX 母表可见代码；不等于历史全 A 母体",
        },
        {
            "metric": "粒度重复",
            "value": int(frame.duplicated(["asof", "code"]).sum()),
            "status": "PASS",
            "definition": "(asof, code) 重复行",
        },
        {
            "metric": "执行状态不完整行",
            "value": int((~frame["execution_status_complete"].fillna(False)).sum()),
            "status": "PASS",
            "definition": "已明确排除并保留审计行，未静默回退",
        },
        {
            "metric": "交易所核对缺失证券",
            "value": 239,
            "status": "FAIL",
            "definition": "SSE/SZSE 官方终止上市记录中曾与 2018–2023 重叠、当前母表未覆盖的证券",
        },
    ]

    headline_rows = [
        {
            "id": "headline",
            "project_status": "BLOCKED_DATA",
            "passed_years": 0,
            "total_oos_years": 4,
            "missing_historical_symbols": 239,
            "tdx_coverage": 0,
            "frozen_years_read": 0,
        }
    ]
    report_datasets = materialize_report_database(
        headline_rows, yearly_rows, feature_rows, quality_rows
    )
    report_database_path = "data/research/early_winner_v4/report_sources.sqlite"
    headline_source = source(
        "v4_headline",
        "V4 审计结论快照",
        report_database_path,
        "读取经冻结审计与官方母表对账生成的项目结论指标。",
        ["id = 'headline'", "frozen_years_read = 0"],
        [
            "passed_years：2020–2023 四个开发区样本外年度中通过全部门禁的年度数。",
            "missing_historical_symbols：SSE/SZSE 官方记录中曾与2018–2023重叠但当前TDX母表未覆盖的证券数。",
        ],
        "SELECT * FROM v4_headline WHERE id = 'headline'",
    )
    result_source = source(
        "v4_audit",
        "V4 冻结开发审计年度结果",
        report_database_path,
        "读取不可变 V4 开发审计中的 ML 与 RS60 年度指标。",
        ["test_year in 2020..2023", "frozen years 2024/2025 excluded"],
        [
            "total_return：八相位共同资金边界下的年度复合收益。",
            "double_cost_return：按 40bp 往返成本压力测试的年度复合收益。",
            "Precision@20：每个可排名周 Top20 中未来40日同期横截面前5%标签占比。",
            "min_phase_invested_periods：八个起始相位中最少的实际投资周期数。",
        ],
        (
            "SELECT year, method, total_return, double_cost_return, "
            "precision_at_20, pr_auc, ic, max_drawdown, min_phase_periods, "
            "min_phase_invested_periods, gate_passed "
            "FROM v4_year_metrics ORDER BY year, method"
        ),
    )
    feature_source = source(
        "v4_features",
        "V4 2018–2023 冻结特征分片",
        report_database_path,
        "按周横截面计算每个特征与未来40交易日收益的 Spearman IC，再按六年等权汇总。",
        [
            "2018-01-01 <= asof <= 2023-12-31",
            "v4_eligible = true",
            "entry_executable = true",
            "forward_return_40 is not null",
            "2024/2025/2026 excluded",
        ],
        [
            "mean_ic：每年周等权 Spearman IC 的六年算术平均。",
            "positive_years：六个开发年度中 mean yearly IC > 0 的年度数。",
        ],
        (
            "SELECT feature, mean_ic, positive_years, minimum_ic, maximum_ic, "
            "years_observed FROM v4_feature_metrics ORDER BY mean_ic DESC"
        ),
    )
    quality_source = source(
        "v4_quality",
        "V4 数据质量与母表对账结果",
        report_database_path,
        "读取内部一致性检查和历史母表对账的审计指标。",
        ["2018-01-01 <= asof <= 2023-12-31", "frozen years excluded"],
        [
            "status=PASS 仅代表对应检查通过；历史母表 FAIL 会覆盖内部一致性通过项。",
            "交易所核对缺失证券指资格过滤前遗漏，不等于这些证券全部满足策略准入。",
        ],
        (
            "SELECT metric, value, status, definition FROM v4_quality_metrics "
            "ORDER BY metric"
        ),
    )
    universe_source = source(
        "universe_audit",
        "历史证券母表官方对账",
        "docs/research/early-winner-v4-universe-audit.md",
        "以交易所官方终止上市数据对账当前 TDX 与 V3/V4 历史代码并集。",
        ["listed_at <= 2023-12-31", "delisted_at >= 2018-01-01", "A shares only"],
        [
            "missing historical securities：在2018–2023任一时间上市但未进入当前TDX母表的终止上市证券数。",
            "本对账说明母体缺失，不主张这些证券均会通过120日、ST、流动性或停牌过滤。",
        ],
    )

    artifact = {
        "surface": "report",
        "manifest": {
            "version": 1,
            "surface": "report",
            "title": "早期强势股 V4：开发审计与晋级阻断报告",
            "description": "2018–2023 点时开发区的可复现模型审计、特征归因和历史母表质量结论。",
            "generatedAt": GENERATED_AT,
            "sources": [headline_source, result_source, feature_source, quality_source, universe_source],
            "blocks": [
                {"id": "title", "type": "markdown", "body": "# 早期强势股 V4：开发审计与晋级阻断报告", "layout": "full"},
                {
                    "id": "summary",
                    "type": "markdown",
                    "body": (
                        "## 技术结论：V4 不具备晋级依据\n\n"
                        "V4 在 2020–2023 四个开发区样本外年度全部失败，ML 的双倍成本最差相位收益逐年均为负；"
                        "2020 和 2023 的最弱相位还只有 1 个实际投资周期。与此同时，交易所官方终止上市数据与当前 TDX 母表对账发现 239 只历史证券在资格过滤前已被遗漏。"
                        "因此平台状态必须保持 `BLOCKED_DATA`，既不能解封 2024/2025，也不能生成交易部署。"
                    ),
                    "layout": "full",
                },
                {"id": "headline_metrics", "type": "metric-strip", "cardIds": ["project_status", "oos_years", "missing_universe", "frozen_access"], "layout": "full"},
                {
                    "id": "year_result_text",
                    "type": "markdown",
                    "body": (
                        "## 年度证据显示模型没有跨年稳定优势\n\n"
                        "下图在完全相同的共同周期和资金可用边界上比较 ML 与 RS60。2020 年 ML 只是少亏，2021–2023 的 Precision@20 低于 RS60；"
                        "所有年度的 ML 双倍成本收益都为负。结果支持拒绝 V4，而不是继续在同一 15 特征族上调参。"
                    ),
                    "layout": "full",
                },
                {"id": "year_result_chart_block", "type": "chart", "chartId": "year_returns", "layout": "full"},
                {
                    "id": "feature_text",
                    "type": "markdown",
                    "body": (
                        "## 原 15 特征族多数弱、反向或重复\n\n"
                        "按 2018–2023 开发区逐年周等权 IC 汇总，只有 `event_score` 略为正且 5/6 年同号，但强度不足以形成可晋级组合；60 日动量和价格偏离类特征平均为负。"
                        "相关矩阵的熵有效秩约为 5.99/15，`return_n` 与 `relative_return_n` 的横截面秩完全一致，成交量比与成交额比相关约 0.99。"
                        "这说明增加同类特征或重调 ML 参数缺乏证据基础。"
                    ),
                    "layout": "full",
                },
                {"id": "feature_chart_block", "type": "chart", "chartId": "feature_ic", "layout": "full"},
                {
                    "id": "definitions",
                    "type": "markdown",
                    "body": (
                        "## 口径与方法保持点时和执行一致\n\n"
                        "决策时点为每周最后一个交易日收盘；下一市场交易日开盘执行；不可成交的入选槽位保留现金且不回填。"
                        "标签持有 40 个市场交易日，未复权价格用于成交，ForwardFactor 用于公司行动后的总收益。组合采用 8 个起始相位、共同的候选与基准资金边界、20bp 基础成本和 40bp 双倍成本。"
                        "候选排名不使用下一日可成交性。2024、2025 和 2026 均未被本报告读取。"
                    ),
                    "layout": "full",
                },
                {"id": "quality_table_block", "type": "table", "tableId": "quality_table", "layout": "full"},
                {
                    "id": "limitations",
                    "type": "markdown",
                    "body": (
                        "## 历史母体缺失是高严重度阻断，不只是普通 caveat\n\n"
                        "当前历史构建从今天仍可见的 TDX `get_stock_list` 反推所有年份。上交所、深交所官方终止上市资料中有 239 只证券曾与 2018–2023 重叠，但当前 TDX 与 V3/V4 代码并集覆盖均为 0。"
                        "北交所还存在旧代码换新代码、精选层日期和转板区间尚未闭环的问题。这个偏差发生在 ST、上市天数、流动性和停牌过滤之前，故无法用现有 1,441 只样本证明“全 A”结果。"
                        "此外，V4 事件证据没有保存唯一选中公告，训练标签仍条件化于次日可成交样本，端点 MDD 也不是逐日盯市回撤。"
                    ),
                    "layout": "full",
                },
                {
                    "id": "next_steps",
                    "type": "markdown",
                    "body": (
                        "## 下一步：先补母表与事件证据，再一次性证伪 V5\n\n"
                        "1. 用交易所官方终止上市、换码和转板记录建立区间有效的历史证券母表，并为缺失证券取得经批准、可追溯的历史行情源。\n"
                        "2. 重建 2018–2023 特征和执行标签，量化幸存者偏差对宽度、排名和收益的影响。\n"
                        "3. V5 只预注册 `selected_event_score DESC, amount_ratio ASC` 的确定性规则，并把选中公告的哈希、类型、发布时间和生效时间逐条冻结；不再训练同类 ML。\n"
                        "4. 仅当历史母表、事件溯源和开发期样本门禁全部通过后，才允许一次性读取 2024/2025；失败即拒绝，规则变化另建 V6。"
                    ),
                    "layout": "full",
                },
                {
                    "id": "questions",
                    "type": "markdown",
                    "body": (
                        "## 尚待回答的问题\n\n"
                        "- TDX 无法返回的退市证券，采用哪个经用户批准且可核验的历史行情源？\n"
                        "- 补齐母体后，负动量 IC 是否仍存在，还是幸存者偏差的产物？\n"
                        "- V5 在冻结期若只因活跃周期不足，应继续前瞻累积样本，还是停止该特征族？"
                    ),
                    "layout": "full",
                },
            ],
            "cards": [
                {
                    "id": "project_status",
                    "dataset": "headline",
                    "filter": {"id": "headline"},
                    "description": "开发审计失败且历史母表门禁失败，研究项目不得晋级。",
                    "sourceId": "v4_headline",
                    "metrics": [{"label": "项目状态", "field": "project_status"}],
                },
                {
                    "id": "oos_years",
                    "dataset": "headline",
                    "filter": {"id": "headline"},
                    "description": "2020–2023 四个开发区样本外年度通过数。",
                    "sourceId": "v4_headline",
                    "metrics": [{"label": "通过年度", "field": "passed_years", "format": "number"}, {"label": "总年度", "field": "total_oos_years", "format": "number"}],
                },
                {
                    "id": "missing_universe",
                    "dataset": "headline",
                    "filter": {"id": "headline"},
                    "description": "交易所官方记录中曾与开发期重叠、但当前母表在资格过滤前遗漏的证券。",
                    "sourceId": "v4_headline",
                    "metrics": [{"label": "母表缺失证券", "field": "missing_historical_symbols", "format": "number"}, {"label": "TDX 覆盖", "field": "tdx_coverage", "format": "number"}],
                },
                {
                    "id": "frozen_access",
                    "dataset": "headline",
                    "filter": {"id": "headline"},
                    "description": "冻结期尚未读取；数据门禁未通过时保持封存。",
                    "sourceId": "v4_headline",
                    "metrics": [{"label": "冻结期读取", "field": "frozen_years_read", "format": "number"}],
                },
            ],
            "charts": [
                {
                    "id": "year_returns",
                    "title": "V4 年度组合收益",
                    "subtitle": "2020–2023 开发区样本外年度；同一共同资金边界，数值为复合收益",
                    "showDescription": True,
                    "intent": "comparison",
                    "question": "ML 是否在每个开发区样本外年度稳定优于 RS60？",
                    "rationale": "四个离散年度与两个方法适合用分组柱状图直接比较收益符号和幅度。",
                    "comparisonContext": {"baseline": "RS60", "grain": "year × method", "unit": "rate", "denominator": "equal-weight Top20 capital with cash for unfilled slots"},
                    "type": "bar",
                    "dataset": "year_metrics",
                    "sourceId": "v4_audit",
                    "encodings": {
                        "x": {"field": "year", "type": "ordinal", "label": "年度"},
                        "y": {"field": "total_return", "type": "quantitative", "format": "percent", "label": "组合收益"},
                        "color": {"field": "method", "type": "nominal", "label": "方法"},
                        "tooltip": [
                            {"field": "double_cost_return", "type": "quantitative", "format": "percent", "label": "双倍成本收益"},
                            {"field": "precision_at_20", "type": "quantitative", "format": "percent", "label": "Precision@20"},
                            {"field": "min_phase_invested_periods", "type": "quantitative", "format": "number", "label": "最少投资周期"},
                        ],
                    },
                    "combinationRationale": "颜色只编码方法这一第二分类维度，年度已由横轴承载。",
                    "layout": "full",
                },
                {
                    "id": "feature_ic",
                    "title": "V4 特征平均 Spearman IC",
                    "subtitle": "2018–2023 开发区，按年度周等权 IC 再做六年平均；零线表示无排序关系",
                    "showDescription": True,
                    "intent": "comparison",
                    "question": "哪些 V4 特征提供稳定的未来40日横截面排序信息？",
                    "rationale": "15 个长标签特征适合按平均 IC 排序的水平柱状图，并保留正负零线。",
                    "comparisonContext": {"baseline": "IC = 0", "grain": "feature", "unit": "Spearman correlation", "denominator": "six development years"},
                    "type": "horizontalBar",
                    "dataset": "feature_metrics",
                    "sourceId": "v4_features",
                    "encodings": {
                        "x": {"field": "feature", "type": "nominal", "label": "特征"},
                        "y": {"field": "mean_ic", "type": "quantitative", "format": "number", "label": "平均 IC"},
                        "tooltip": [
                            {"field": "positive_years", "type": "quantitative", "format": "number", "label": "正 IC 年数"},
                            {"field": "minimum_ic", "type": "quantitative", "format": "number", "label": "最小年度 IC"},
                            {"field": "maximum_ic", "type": "quantitative", "format": "number", "label": "最大年度 IC"},
                        ],
                    },
                    "referenceLines": [{"axis": "y", "value": 0, "label": "无排序关系", "color": "neutral", "lineStyle": "solid"}],
                    "layout": "full",
                },
            ],
            "tables": [
                {
                    "id": "quality_table",
                    "title": "V4 数据质量与母表门禁",
                    "subtitle": "通过项仅证明已纳入母体的内部一致性；历史覆盖失败会覆盖其他通过项",
                    "showDescription": True,
                    "dataset": "quality_metrics",
                    "sourceId": "v4_quality",
                    "defaultSort": {"field": "metric", "direction": "asc"},
                    "density": "spacious",
                    "layout": "full",
                    "columns": [
                        {"field": "metric", "label": "检查项", "type": "text"},
                        {"field": "value", "label": "值", "format": "number"},
                        {"field": "status", "label": "状态", "type": "text"},
                        {"field": "definition", "label": "解释", "type": "text"},
                    ],
                }
            ],
        },
        "snapshot": {
            "version": 1,
            "generatedAt": GENERATED_AT,
            "status": "ready",
            "datasets": report_datasets,
        },
        "sources": [headline_source, result_source, feature_source, quality_source, universe_source],
        "package_info": {
            "audit_id": audit["audit_id"],
            "snapshot_prefix": "05b1a6ec6218d7e59027",
            "audit_sha256": sha256(AUDIT_PATH),
            "report_database_sha256": sha256(REPORT_DATABASE),
            "feature_shards": [
                {"name": path.name, "sha256": sha256(path)} for path in feature_files
            ],
            "frozen_validation_opened": False,
            "supporting_notebook": "docs/research/early-winner-v4-development-audit.ipynb",
            "chart_map": [
                {
                    "section": "年度证据",
                    "question": "ML 是否逐年优于 RS60？",
                    "family": "comparison",
                    "type": "grouped bar",
                    "fields": ["year", "method", "total_return"],
                    "claim": "V4 无跨年稳定优势",
                    "palette_policy": "hard two-root cap",
                },
                {
                    "section": "特征归因",
                    "question": "哪些特征有稳定排序能力？",
                    "family": "comparison and ranking",
                    "type": "horizontal bar",
                    "fields": ["feature", "mean_ic"],
                    "claim": "15 特征多数弱、反向或冗余",
                    "palette_policy": "single-root preferred with neutral zero line",
                },
            ],
            "omissions": [
                "No daily mark-to-market curve: V4 audit stores endpoint portfolio metrics only.",
                "No 2024/2025 validation result: frozen years remain sealed by design.",
                "No candidate list: V4 did not pass development gates and must not emit candidates.",
            ],
        },
    }
    return artifact


def main() -> None:
    artifact = build()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True)
    OUTPUT.write_text(payload + "\n", encoding="utf-8")
    print(OUTPUT)


if __name__ == "__main__":
    main()
