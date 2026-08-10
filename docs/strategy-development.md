# 策略插件开发

## 最小契约

内置策略放在 `research_platform/strategies/`；独立策略优先放在 `strategy_plugins/<strategy_id>/`，不再修改平台注册表。每个本地插件包含 `plugin.json` 和一个返回策略实例的入口工厂，并实现 `scan(**context) -> StrategyScanResult`。可从 Web“策略”页热重载；加载失败会被隔离并显示，不影响其他策略。

插件的 `StrategyMetadata` 必须使用稳定 `strategy_id`、语义化版本、`plugin_api_version="1"` 和 `runtime_adapter="generic_daily"`，并声明：

- `asset_classes`：例如 `A_STOCK`、`US_EQUITY`。
- `execution_model`：`SINGLE_LEG` 或 `MULTI_LEG`。
- `supports_short` 与 `requires_approval`。
- `strategy_family`、`lifecycle`、`scan_enabled` 与 `backtest_enabled`。
- 每项 `DataRequirement` 的数据集、频率、复权、回看长度、必需性和字段。

单腿策略输出 `PlatformSignal`；多腿策略输出 `OrderGroupIntent`，每个交易腿使用 `OrderLegIntent`。不得在策略中调用下单、写数据库或临时抓取外部数据。

## 接入步骤

1. 从 `strategy_plugins/_template/` 建立插件目录，保持清单 ID 与元数据 ID 一致。
2. 只声明 `SourceRegistry` 已登记的数据；新增外部数据必须先实现权威来源和可用时间。
3. 单腿策略返回 `PlatformSignal`，多腿策略返回 `OrderGroupIntent`；不要混用。
4. 在历史循环中仅使用传入的 `asof` 及之前数据，信号在下一交易日开盘执行。
5. 热重载后先做固定快照回测，再加入自定义组合；实验策略保持人工批准和模拟执行。
6. 增加防未来函数、费用、T+1、涨跌停、原子多腿和缺失数据测试。

`generic_daily` 已同时支持单腿和多腿回测。`chan_daily` 与 `course49_daily` 是平台保留适配器，只供内置兼容层使用。本地插件不得声明这两个适配器。

## 组合模式

`capital_sleeves` 为成员分配独立资金，适合逻辑和执行模型不同的策略；`score_fusion` 合并同标的方向与强度；`intersection` 要求多个 Alpha 同向；`risk_overlay` 允许风险策略否决开仓；`comparison` 仅用于同快照对照。

多腿策略只能进入 `capital_sleeves` 或 `comparison`，保证意图原子性。当前只有这两种模式支持回测；`score_fusion`、`intersection` 与 `risk_overlay` 用于扫描层信号叠加。`priority` 按成员优先级决定冲突方向，`net_score` 按权重计算净方向，`risk_first` 优先执行卖出或风险否决。新增组合在 Web“策略”页维护，内置组合不可覆盖或删除。

## 验收

运行 `py -3 -m unittest discover -s research_platform\tests -v`、`pnpm --dir frontend test -- --run` 和 `pnpm --dir frontend build`。真实 TDX 验证必须保持默认只读，只有明确要求时才使用 `--push-tdx`，任何策略都不得调用真实订单接口。

## 生成策略

模型生成策略不走正式源码接入步骤。首版只允许 V3 兼容变体覆盖批准的纯函数 hook，并先进入 `data/strategy_lab/experiments/`。校验和同快照回放通过后，用户可人工晋级到本地 promoted 目录；晋级项只能回测，不能扫描或加入组合。完整约束见 [strategy-lab.md](strategy-lab.md)。
