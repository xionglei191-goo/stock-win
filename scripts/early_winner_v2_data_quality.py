from __future__ import annotations

import json
import sys
from pathlib import Path

import nbformat
import pandas as pd
from nbclient import NotebookClient


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
FEATURE_DIR = REPOSITORY_ROOT / "data" / "research" / "early_winner_v1" / "features"
NOTEBOOK_PATH = REPOSITORY_ROOT / "docs" / "research" / "early-winner-v2-data-quality.ipynb"


def _source_paths() -> list[Path]:
    paths = [
        path
        for path in sorted(FEATURE_DIR.glob("*.parquet"))
        if path.stem[-4:].isdigit() and int(path.stem[-4:]) <= 2023
    ]
    years = [int(path.stem[-4:]) for path in paths]
    if years != list(range(2018, 2024)):
        raise RuntimeError(f"Expected frozen 2018-2023 feature files, found {years}")
    return paths


def build_notebook() -> Path:
    paths = _source_paths()
    relative_paths = [str(path.relative_to(REPOSITORY_ROOT)).replace("\\", "/") for path in paths]
    notebook = nbformat.v4.new_notebook()
    notebook["metadata"]["kernelspec"] = {
        "display_name": "Python 3",
        "language": "python",
        "name": "python3",
    }
    notebook["metadata"]["language_info"] = {"name": "python", "version": sys.version.split()[0]}
    notebook["cells"] = [
        nbformat.v4.new_markdown_cell(
            """# 早期强势股 V2：2018—2023 数据质量审计

## tl;dr

- 数据粒度是“周度决策日 × 股票”，155,107 行、307 个决策日、无重复主键。
- V1 数据中 103,492 行没有可执行的 60 日结果；V2 只在其余 51,615 行中构造标签，且经过股票池门禁后实际训练标签为 49,700 行。
- `turnover_20` 在开发期全空，`valuation_percentile` 恒为 0.5；资金因子存在明显覆盖漂移和极端量纲值。
- 发布时间与生效时间审计通过。数据可用于修复后的开发期实验，但不能原样复用 V1 标签和全量特征。"""
        ),
        nbformat.v4.new_markdown_cell(
            """## Context & Methods

### Key Assumptions

- 只读取 V1 已冻结的 2018—2023 年度特征文件；2024、2025 和 2026 不参与本 notebook。
- 决策时点为每个 `asof` 当日 15:00。
- `published_at` 与 `effective_at` 必须不晚于决策时点。
- V2 标签只对 `entry_executable=true` 且 `forward_return_60` 非空的合格股票生成。"""
        ),
        nbformat.v4.new_code_cell(
            "from pathlib import Path\n"
            "import pandas as pd\n"
            "import numpy as np\n"
            "from IPython.display import display\n"
            f"REPOSITORY_ROOT = Path({json.dumps(str(REPOSITORY_ROOT))})\n"
            f"SOURCE_PATHS = {[item for item in relative_paths]!r}\n"
            "frames = [pd.read_parquet(REPOSITORY_ROOT / path) for path in SOURCE_PATHS]\n"
            "data = pd.concat(frames, ignore_index=True)\n"
            "data['year'] = pd.to_datetime(data['asof']).dt.year\n"
            "assert set(data['year']) == set(range(2018, 2024))\n"
            "assert not data.duplicated(['asof', 'code']).any()\n"
            "len(data)"
        ),
        nbformat.v4.new_markdown_cell("## Data\n\n### 1. Confirm grain and yearly volume"),
        nbformat.v4.new_code_cell(
            "year_profile = data.groupby('year').agg(rows=('code','size'), decision_dates=('asof','nunique'), stocks=('code','nunique'), executable_rows=('entry_executable','sum')).reset_index()\n"
            "display(year_profile)\n"
            "grain = {'rows': len(data), 'columns': len(data.columns), 'decision_dates': data['asof'].nunique(), 'stocks': data['code'].nunique(), 'duplicate_keys': int(data.duplicated(['asof','code']).sum())}\n"
            "grain"
        ),
        nbformat.v4.new_markdown_cell("## Results\n\n### 2. Label scope is the highest-impact V1 issue"),
        nbformat.v4.new_code_cell(
            "outcome = pd.to_numeric(data['forward_return_60'], errors='coerce')\n"
            "label_scope = pd.crosstab(data['entry_executable'], outcome.notna(), margins=True)\n"
            "display(label_scope)\n"
            "assert ((~data['entry_executable']) == outcome.isna()).all()\n"
            "{'rows_without_outcome': int(outcome.isna().sum()), 'rows_with_outcome': int(outcome.notna().sum())}"
        ),
        nbformat.v4.new_markdown_cell("### 3. Missingness and distribution drift remove several features from V2"),
        nbformat.v4.new_code_cell(
            "feature_columns = ['northbound_change_ratio','institution_holding_change_ratio','turnover_20','valuation_percentile']\n"
            "missing = data.groupby('year')[feature_columns].agg(lambda s: pd.to_numeric(s, errors='coerce').isna().mean()).round(4)\n"
            "display(missing)\n"
            "distribution = pd.DataFrame({column: {'nonmissing_rate': pd.to_numeric(data[column], errors='coerce').notna().mean(), 'unique': pd.to_numeric(data[column], errors='coerce').nunique(dropna=True), 'p01': pd.to_numeric(data[column], errors='coerce').quantile(.01), 'p99': pd.to_numeric(data[column], errors='coerce').quantile(.99), 'maximum': pd.to_numeric(data[column], errors='coerce').max()} for column in feature_columns}).T\n"
            "display(distribution)"
        ),
        nbformat.v4.new_markdown_cell("### 4. Point-in-time timestamps pass their hard audit"),
        nbformat.v4.new_code_cell(
            "decision = pd.to_datetime(data['asof']) + pd.Timedelta(hours=15)\n"
            "published = pd.to_datetime(data['published_at'], errors='coerce')\n"
            "effective = pd.to_datetime(data['effective_at'], errors='coerce')\n"
            "time_audit = {'published_missing': int(published.isna().sum()), 'published_after_decision': int((published > decision).sum()), 'effective_missing': int(effective.isna().sum()), 'effective_after_decision': int((effective > decision).sum())}\n"
            "assert all(value == 0 for value in time_audit.values())\n"
            "time_audit"
        ),
        nbformat.v4.new_markdown_cell(
            """## Takeaways

1. **High severity / high confidence:** V2 必须排除无执行结果的行，不能把它们默认标为 0。
2. **High severity / high confidence:** 每个训练折都应删除全空、近空和常量特征；截尾与中位数只能由训练折计算。
3. **High severity / medium confidence:** 资金因子需要独立核对量纲和历史覆盖；在完成前，技术/行业核心方案不依赖它们。
4. **通过项:** 点时连接与主键粒度可以作为 V2 开发期输入。
5. **边界:** 本审计不证明策略有效，也没有打开 2024/2025 或 2026。"""
        ),
    ]
    NOTEBOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    nbformat.write(notebook, NOTEBOOK_PATH)
    client = NotebookClient(notebook, timeout=600, kernel_name="python3")
    client.execute(cwd=str(REPOSITORY_ROOT))
    nbformat.write(notebook, NOTEBOOK_PATH)
    return NOTEBOOK_PATH


if __name__ == "__main__":
    print(build_notebook())
