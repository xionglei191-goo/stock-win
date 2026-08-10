# Repository Guidelines

## Project Structure & Module Organization

`research_platform/` is the maintained application: data adapters, built-in strategies, portfolio simulation, backtests, storage, FastAPI, and CLI. Put trusted local strategy plugins in `strategy_plugins/<strategy_id>/` using the API V1 manifest template; keep platform-owned strategies in `research_platform/strategies/` and tests in `research_platform/tests/`. `frontend/` contains the React/Vite workspace. `strategy_v1/` remains the Chan V1 compatibility layer and low-level algorithms.

`tdx-mock/` and `new_tdx64/` are local TongdaXin installations and data stores. Treat their binaries, market data, and configuration as external runtime assets, not application source.

## Build, Test, and Development Commands

Run Python commands from `D:\Project\stock`:

```powershell
py -3 -m unittest discover -s strategy_v1\tests -v
py -3 -m unittest discover -s research_platform\tests -v
py -3 -m research_platform doctor
py -3 -m research_platform scan --max-stocks 300
py -3 -m research_platform backtest --strategy combined
py -3 -m research_platform serve
```

Build and test the dashboard from `frontend/`:

```powershell
pnpm test -- --run
pnpm build
```

## Coding Style & Naming Conventions

Use four-space indentation, UTF-8, type hints, and `from __future__ import annotations`. Use `snake_case` for functions/modules, `PascalCase` for classes, and uppercase names for constants. Keep data access, signal generation, and execution logic separate. Every signal must depend only on data available at its timestamp. No formatter or linter is configured; keep imports grouped and changes focused.

## Testing Guidelines

Python tests use `unittest` and `test_*.py`; frontend tests use Vitest and Testing Library. Add focused coverage for signal contracts, ranking rules, execution constraints, API responses, and especially lookahead prevention. Mock TDX in unit tests; keep live-client checks explicit and non-trading.

## Commit & Pull Request Guidelines

The workspace currently has no Git history from which to infer conventions. Use short, imperative commit subjects, optionally prefixed by area, such as `market: fix sector breadth ranking`. Pull requests should explain behavior changes, data assumptions, verification commands, and generated-output impact. Include screenshots only for TongdaXin or dashboard display changes.

## Safety & Configuration

Scans are local by default. Use `--push-tdx` only for custom blocks, warnings, and chart signals. Never call real order APIs or commit account details, `data/`, client configuration, snapshots, or downloaded market data. Treat adjusted bars as analytical inputs and raw bars as execution inputs. Initialize TQ through the configured `PYPlugins\user` path and close it reliably.
