# Local Strategy Plugins

Create one directory per trusted local strategy. A plugin is loaded only when the directory contains `plugin.json`.

```text
strategy_plugins/
  my_arbitrage_v1/
    plugin.json
    strategy.py
```

Copy the two files in `_template/`, remove the `.example` suffix, and keep `strategy_id` identical in the manifest and `StrategyMetadata`. Local plugins must use plugin API `1` and the `generic_daily` runtime adapter.

Reload from the Strategy page or call:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/strategy-catalog/reload
```

Plugin code runs in the local platform process and is therefore trusted code. It must not place orders, write the research database, fetch undeclared data, or read future rows. Keep external credentials outside this directory.
