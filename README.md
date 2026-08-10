# 通达信多策略投研平台

本项目以本地通达信为首要行情与板块数据底座，在其上建立可复现的数据契约、策略插件、组合编排、Python 回测、模拟组合和 Web 展示。缠论、“49 课”和配对套利是彼此独立的策略插件；平台不调用真实下单接口。

## 当前能力

- 通达信全 A 股、日线、板块及涨跌停数据适配。
- 数据健康检查、SQLite 元数据、Parquet 研究快照和来源登记。
- 策略驱动 `DataPlan`、历史区间读取、24GB 内存/50GB 磁盘两级缓存、8线程本地处理和三作业调度；TQ 始终单通道。
- `chan_v1`：市场/板块/龙头过滤后的日线中枢突破与退出。
- `course49_v1/v2/v3`：题材龙头基线、市场风格自适应版与局部加速核心实验版，买入信号必须人工批准。
- `pairs_arbitrage_v1`：固定同行业股票对的价差均值回归研究，使用原子多腿意图和仅模拟做空。
- API V1 本地策略插件、热重载、数据依赖声明和自定义组合；支持资金分舱、信号加权、交集确认和风险覆盖。
- 通用日线单腿/多腿回测适配器；新增普通策略或套利策略不需要修改扫描和回测主流程。
- 分策略资金隔离、原子多腿模拟执行、T+1、涨跌停、费用、滑点和组合上限。
- FastAPI + React 本地工作台，支持可配置回测、指标卡、策略对比、逐笔成交与持仓变化，以及显式的通达信自定义板块、预警和指标回传。
- OpenAI 结构化研究助手：扫描后日报、候选证据意见、人工审批记录和事后反馈。
- 隔离的 `course49_v3` 自动实验室：生成兼容变体、同快照回放、成本压力测试和人工晋级。

## 快速开始

在 `D:\Project\stock` 下运行：

```powershell
py -3 -m research_platform doctor
py -3 -m unittest discover -s research_platform\tests -v
py -3 -m unittest discover -s strategy_v1\tests -v

cd frontend
pnpm install
pnpm test -- --run
pnpm build
cd ..

py -3 -m research_platform catalog
py -3 -m research_platform scan --strategies adaptive_multi_strategy
py -3 -m research_platform backtest --strategy adaptive_multi_strategy --daily-bars 180
py -3 -m research_platform diagnose-course49 --backtest-id <BACKTEST_ID>
py -3 -m research_platform daily-research
py -3 -m research_platform refresh-feedback
py -3 -m research_platform serve
```

打开 `http://127.0.0.1:8000`。扫描默认只写本地研究库；仅在确认需要更新通达信客户端时增加 `--push-tdx`。

## 目录

- `research_platform/`：平台服务、策略、存储、回测与 API。
- `frontend/`：React 投研工作台。
- `strategy_v1/`：原缠论 MVP 与兼容算法模块。
- `strategy_plugins/`：可信本地策略插件、清单和可复制模板。
- `docs/`：架构、数据契约、策略、回测、运维与路线图。
- `data/`：运行数据库和 Parquet 快照，不提交版本库。
- `tdx-mock/`、`new_tdx64/`：本机通达信资产，不视为应用源码。

详细设计见 [docs/architecture.md](docs/architecture.md)，研究助手见 [docs/ai-research.md](docs/ai-research.md)，自动实验见 [docs/strategy-lab.md](docs/strategy-lab.md)，新增策略流程见 [docs/strategy-development.md](docs/strategy-development.md)，日常操作见 [docs/operations.md](docs/operations.md)。
