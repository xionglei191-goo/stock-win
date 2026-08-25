"""Replay the five frozen Chan windows with the reworked structure algorithm.

Runs against the same immutable snapshots as the V1 audit so the comparison is
apples-to-apples. Writes results next to the frozen artifacts under a separate
filename; the frozen V1 artifacts are never modified.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from research_platform.backtest_engine import BacktestService
from research_platform.chan_research import load_chan_protocol
from research_platform.config import PlatformConfig
from research_platform.storage import Database

BASELINE_V1 = {
    "2021-04-01:2022-04-29": -0.0822,
    "2022-05-01:2023-05-31": -0.1000,
    "2023-06-01:2024-06-28": -0.2087,
    "2024-07-01:2025-07-24": -0.0652,
    "2025-07-25:2026-08-07": -0.0780,
}


def main() -> int:
    config = PlatformConfig()
    database = Database(config)
    protocol, protocol_hash = load_chan_protocol(
        Path("data/research/chan_v1/protocol.json")
    )
    service = BacktestService(config, database)

    rows: list[dict[str, object]] = []
    chained_v2 = 1.0
    chained_v1 = 1.0
    for window in protocol["windows"]:
        key = f"{window['start_date']}:{window['end_date']}"
        result = service.replay_chan(
            str(window["source_backtest_id"]),
            strategy_id="chan_v1",
            start_date=str(window["start_date"]),
            end_date=str(window["end_date"]),
            execution_cost_multiplier=float(
                protocol["cost_multipliers"]["baseline"]
            ),
        )
        metrics = result.get("metrics", result)
        total_return = float(metrics.get("total_return", 0.0))
        rows.append(
            {
                "window": key,
                "v2_return": total_return,
                "v1_return": BASELINE_V1.get(key, float("nan")),
                "max_drawdown": float(metrics.get("max_drawdown", 0.0)),
                "trades": int(metrics.get("trades", 0)),
                "win_rate": float(metrics.get("win_rate", 0.0)),
                "backtest_id": str(result.get("backtest_id", "")),
            }
        )
        chained_v2 *= 1.0 + total_return
        chained_v1 *= 1.0 + BASELINE_V1.get(key, 0.0)
        service.cache.memory.clear()
        print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    summary = {
        "protocol_hash": protocol_hash,
        "windows": rows,
        "v2_chained_return": chained_v2 - 1.0,
        "v1_chained_return": chained_v1 - 1.0,
        "v2_profitable_windows": sum(
            1 for row in rows if float(row["v2_return"]) > 0
        ),
        "window_count": len(rows),
    }
    destination = Path("data/research/chan_v1/v2_structure_comparison.json")
    destination.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
