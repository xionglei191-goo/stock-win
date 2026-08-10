from __future__ import annotations

import numpy as np
import pandas as pd


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    values = pd.to_numeric(close, errors="coerce").astype(float)
    fast_line = values.ewm(span=fast, adjust=False, min_periods=fast).mean()
    slow_line = values.ewm(span=slow, adjust=False, min_periods=slow).mean()
    diff = fast_line - slow_line
    dea = diff.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = (diff - dea) * 2.0
    return pd.DataFrame({"diff": diff, "dea": dea, "histogram": histogram}, index=close.index)


def percentile_rank(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.notna().sum() <= 1:
        return pd.Series(np.where(numeric.notna(), 1.0, np.nan), index=values.index, dtype=float)
    return numeric.rank(method="average", pct=True)
