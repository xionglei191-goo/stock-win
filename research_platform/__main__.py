from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import uvicorn

from .api import create_app
from .ai_research import AIResearchService
from .backtest_engine import BacktestService
from .config import PlatformConfig
from .course49_diagnostics import diagnose_backtest
from .feedback import FeedbackService
from .service import PlatformService
from .validation import validate_course49


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="TongdaXin multi-strategy research platform")
    subcommands = parser.add_subparsers(dest="command", required=True)

    subcommands.add_parser("doctor", help="Check local runtime and TDX connectivity")
    subcommands.add_parser("catalog", help="List strategy plugins and configured groups")

    sync = subcommands.add_parser("sync", help="Create a daily-data research snapshot")
    sync.add_argument("--daily-bars", type=int, default=120)
    sync.add_argument("--refresh-sectors", action="store_true")
    sync.add_argument("--refresh-data", action="store_true")

    scan = subcommands.add_parser("scan", help="Run research or paper scan")
    scan.add_argument("--strategies", default="combined")
    scan.add_argument("--mode", choices=("research", "paper"), default="research")
    scan.add_argument("--push-tdx", action="store_true")
    scan.add_argument("--refresh-sectors", action="store_true")
    scan.add_argument("--max-stocks", type=int)
    scan.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    scan.add_argument("--sample-seed", type=int, default=49)
    scan.add_argument("--refresh-data", action="store_true")

    daily = subcommands.add_parser("daily-research", help="Run a scan and generate an AI research brief")
    daily.add_argument("--strategies", default="combined")
    daily.add_argument("--refresh-sectors", action="store_true")
    daily.add_argument("--max-stocks", type=int)
    daily.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    daily.add_argument("--sample-seed", type=int, default=49)
    daily.add_argument("--refresh-data", action="store_true")

    brief = subcommands.add_parser("generate-brief", help="Generate a brief for an existing scan")
    brief.add_argument("--run-id", required=True)

    subcommands.add_parser("refresh-feedback", help="Refresh decision outcomes")

    backtest = subcommands.add_parser("backtest", help="Run the Python primary backtest")
    backtest.add_argument("--strategy", default="combined")
    backtest.add_argument("--start")
    backtest.add_argument("--end")
    backtest.add_argument("--daily-bars", type=int, default=180)
    backtest.add_argument("--max-stocks", type=int)
    backtest.add_argument("--sampling-mode", choices=("full", "stratified"), default="full")
    backtest.add_argument("--sample-seed", type=int, default=49)
    backtest.add_argument(
        "--universe",
        choices=("all_a", "main_board", "growth", "star", "beijing", "custom"),
        default="all_a",
    )
    backtest.add_argument("--codes", default="", help="Comma-separated codes for the custom universe")
    backtest.add_argument("--refresh-sectors", action="store_true")
    backtest.add_argument("--refresh-data", action="store_true")
    backtest.add_argument(
        "--playbooks",
        default="",
        help="Comma-separated Course49 playbook ids for research backtests",
    )
    backtest.add_argument(
        "--execution-cost-multiplier",
        type=float,
        default=1.0,
        help="Scale Course49 commission, tax, and slippage for stress testing",
    )

    replay = subcommands.add_parser(
        "backtest-replay",
        help="Replay a Course49 backtest from an immutable saved snapshot",
    )
    replay.add_argument("--source-backtest-id", required=True)
    replay.add_argument("--strategy")
    replay.add_argument("--start")
    replay.add_argument("--end")
    replay.add_argument("--execution-cost-multiplier", type=float, default=1.0)

    for command in ("validate-course49", "validate-course49-v3"):
        validate = subcommands.add_parser(
            command,
            help="Evaluate the versioned long-window, forward, and cost-stress gate",
        )
        validate.add_argument("--baseline-backtest-id", required=True)
        validate.add_argument("--stress-backtest-id")
        validate.add_argument("--historical-holdout-backtest-id")
        validate.add_argument("--policy-freeze-date", default="2026-08-09")

    diagnose = subcommands.add_parser(
        "diagnose-course49",
        help="Run an execution-aware reward diagnostic from a saved backtest snapshot",
    )
    diagnose.add_argument("--backtest-id", required=True)
    diagnose.add_argument("--state-strategy")
    diagnose.add_argument("--scope", choices=("snapshot", "backtest"), default="snapshot")
    diagnose.add_argument("--output-dir")

    serve = subcommands.add_parser("serve", help="Serve the API and built React dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    subcommands.add_parser("cache-status", help="Show memory and disk cache usage")
    subcommands.add_parser("cache-prune", help="Apply the configured disk cache limit")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = PlatformConfig()
    service = PlatformService(config)
    if args.command == "doctor":
        _print(service.doctor())
    elif args.command == "catalog":
        _print(service.strategy_catalog())
    elif args.command == "sync":
        _print(
            service.sync_data(
                daily_bars=args.daily_bars,
                refresh_sectors=args.refresh_sectors,
                refresh_data=args.refresh_data,
            )
        )
    elif args.command == "scan":
        report = service.run_scan(
            [item.strip() for item in args.strategies.split(",") if item.strip()],
            mode=args.mode,
            push_tdx=args.push_tdx,
            refresh_sectors=args.refresh_sectors,
            max_stocks=args.max_stocks,
            sampling_mode=args.sampling_mode,
            sample_seed=args.sample_seed,
            refresh_data=args.refresh_data,
        )
        _print(asdict(report))
        return 0 if report.status in ("SUCCEEDED", "BLOCKED_DATA") else 1
    elif args.command == "backtest":
        backtests = BacktestService(config, service.database)
        _print(
            backtests.run(
                args.strategy,
                start_date=args.start,
                end_date=args.end,
                daily_bars=args.daily_bars,
                max_stocks=args.max_stocks,
                universe=args.universe,
                stock_codes=[code.strip() for code in args.codes.split(",") if code.strip()],
                refresh_sectors=args.refresh_sectors,
                sampling_mode=args.sampling_mode,
                sample_seed=args.sample_seed,
                execution_cost_multiplier=args.execution_cost_multiplier,
                refresh_data=args.refresh_data,
                playbook_ids=[
                    item.strip() for item in args.playbooks.split(",") if item.strip()
                ],
            )
        )
    elif args.command == "daily-research":
        _print(
            service.run_daily_research(
                [item.strip() for item in args.strategies.split(",") if item.strip()],
                refresh_sectors=args.refresh_sectors,
                max_stocks=args.max_stocks,
                sampling_mode=args.sampling_mode,
                sample_seed=args.sample_seed,
                refresh_data=args.refresh_data,
            )
        )
    elif args.command == "generate-brief":
        _print(AIResearchService(config, service.database).generate_brief(args.run_id))
    elif args.command == "refresh-feedback":
        _print(FeedbackService(config, service.database).refresh())
    elif args.command == "backtest-replay":
        backtests = BacktestService(config, service.database)
        _print(
            backtests.replay_course49(
                args.source_backtest_id,
                strategy_id=args.strategy,
                start_date=args.start,
                end_date=args.end,
                execution_cost_multiplier=args.execution_cost_multiplier,
            )
        )
    elif args.command in {"validate-course49", "validate-course49-v3"}:
        _print(
            validate_course49(
                service.database,
                args.baseline_backtest_id,
                stress_backtest_id=args.stress_backtest_id,
                historical_holdout_backtest_id=args.historical_holdout_backtest_id,
                policy_freeze_date=args.policy_freeze_date,
            )
        )
    elif args.command == "diagnose-course49":
        _print(
            diagnose_backtest(
                config,
                service.database,
                args.backtest_id,
                state_strategy_id=args.state_strategy,
                scope=args.scope,
                output_dir=Path(args.output_dir) if args.output_dir else None,
            )
        )
    elif args.command == "serve":
        uvicorn.run(create_app(config), host=args.host, port=args.port, reload=args.reload)
    elif args.command == "cache-status":
        _print(service.data_cache.status())
    elif args.command == "cache-prune":
        _print(service.data_cache.prune())
    return 0


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    raise SystemExit(main())
