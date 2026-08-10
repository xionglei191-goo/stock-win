from __future__ import annotations

import argparse
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategy_v1.config import StrategyConfig
from strategy_v1.engine import run_scan
from strategy_v1.tdx_adapter import TdxAdapter


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the V1 TDX/Chan market scan")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Compute and persist locally without updating TDX")
    mode.add_argument("--push-tdx", action="store_true", help="Update the TDX candidate block and warnings")
    parser.add_argument("--refresh-sectors", action="store_true", help="Refresh the cached sector membership map")
    parser.add_argument("--max-stocks", type=int, default=None, help="Diagnostic limit; omitted means all A shares")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = StrategyConfig()
    with TdxAdapter(config, __file__) as adapter:
        result = run_scan(
            adapter,
            config,
            refresh_sectors=args.refresh_sectors,
            max_stocks=args.max_stocks,
        )
        if args.push_tdx:
            buy_codes = [signal.code for signal in result.signals if signal.side == "BUY"]
            adapter.push_candidates(buy_codes)
            adapter.push_warnings(list(result.signals))
            adapter.push_signal_data(list(result.signals))

    print(
        f"market={result.market.regime} breadth={result.market.breadth:.1%} "
        f"sectors={len(result.sectors)} leaders={len(result.leaders)} "
        f"signals={len(result.signals)} equity={result.equity:.2f} positions={result.position_count}"
    )
    for sector in result.sectors:
        print(f"SECTOR {sector.code} {sector.name} score={sector.score:.3f}")
    for signal in result.signals:
        print(f"{signal.timestamp:%Y-%m-%d %H:%M} {signal.side} {signal.code} {signal.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
