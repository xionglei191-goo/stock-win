"""Fail-closed compatibility shim for the retired broad US backtest.

The retired script selected securities from today's surviving TDX files and
today's liquidity, then used that set throughout history.  Importing the module
is harmless for compatibility, but its old universe builder and CLI cannot run.
Use ``us_momentum_backtest.run_backtest`` with a verified ``USBacktestDataset``
loaded from an immutable DATA_READY release.
"""
from __future__ import annotations

from .us_momentum_backtest import StrictUSPointInTimeUniverse, run_backtest


FAIL_CLOSED_MESSAGE = (
    "Legacy broad US momentum backtest is disabled: the current-file universe "
    "is not point-in-time and introduces survivorship/look-ahead bias. Supply "
    "a verified DATA_READY USBacktestDataset to the strict "
    "us_momentum_backtest.run_backtest API."
)


def prefilter_universe(
    min_bars: int = 2500,
    min_dollar_vol: float = 50_000_000,
) -> list[str]:
    del min_bars, min_dollar_vol
    raise RuntimeError(FAIL_CLOSED_MESSAGE)


def main() -> None:
    raise SystemExit(FAIL_CLOSED_MESSAGE)


if __name__ == "__main__":
    main()


__all__ = [
    "FAIL_CLOSED_MESSAGE",
    "StrictUSPointInTimeUniverse",
    "prefilter_universe",
    "run_backtest",
]
