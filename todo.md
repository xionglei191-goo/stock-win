# 美股 PIT 剩余任务实施清单

## 目标与完成标准

在不伪造、不回填、不放松 fail-closed 门禁的前提下，完成：

`assemble-reviewed → prepare-market → build → validate → qualify → us-paper admit-release`

完成标准：

- 最终审查工作区为 `REVIEW_READY`，身份、锚点、季度对账和事件冲突均为 0。
- 最终市场工作区为 `MARKET_READY`，`blocking_gaps=0`。
- release 为 `DATA_READY`，验证无 Critical/High 问题。
- 仅在 `BACKTEST_QUALIFIED` 后执行 paper release admission；若资格失败，诚实停在 `HISTORICAL_FAILED`。
- 不调用真实订单 API；TDX 仅执行只读行情请求。

## 锁定约束

- 研究窗口：`2021-08-01 ~ 2026-07-31`，保留冻结 XNYS warmup 和末端下一执行日。
- 延续已批准 SCOPE-C，不引入外部行情源。
- 必需区间存在未解释 TDX 坏行/缺行的证券采用“质量范围排除”：整只证券退出本次 SCOPE-C 可交易样本，逐证券留痕；绝不填充或直接豁免坏行。
- 282+ warmup 会话属于策略必需历史，不允许 `WARMUP-OHLCV-EXEMPT`。
- 官方发布时间经 payload 验证时使用 `published_at`；否则使用 `max(published_at, observed_at)` 并继续 fail-closed。
- 已冻结的分拆预注册文件不再修改；新增规则另立版本。

## 阶段 1：预注册与清理

- [x] 保存本计划为 `todo.md`。
- [x] 新建并冻结 `docs/us-pit-market-readiness-preregistration-v1.md`。
- [x] 删除 warmup 坏行直接过滤实验。
- [x] 撤销公司行动简单 `continue` 实验，改用统一因果可用时间。

## 阶段 2：可复现 listing alias 审查链

- [x] 增加哈希绑定、人工批准的 alias review 输入。
- [x] 为 CBOE 冻结官方证据并生成唯一 `CBOE.US/BATS` alias。
- [x] 为 GE reverse-split predecessor/successor 生成不重叠 `GE.US` action-lineage alias。
- [x] 废弃临时 `oxalpha_final_v8c` 手工改写，基于权威 review 输入重建。
- [x] exchange canonicalization 使用真实 MIC：`XNYS/XNAS/ARCX/BATS`。
- [x] 安全处理 `pd.NA` 和 `ticker_review/exchange_review` fallback。

## 阶段 3：公司行动因果时间

- [x] 抽取共享 source availability 帮助函数。
- [x] 验证唯一依赖、时间、条款与发布时间证明。
- [x] payload 验证发布时间时允许事后抓取证明历史公开事实。
- [x] 未验证或真正晚到的证据继续 blocking。
- [x] `announced_at > effective_at` 始终 blocking。

## 阶段 4：SCOPE-C 行情质量排除

- [x] 按 vendor code 审计完整必需区间 raw/front 数据。
- [x] 未解释坏行、重复行、非 XNYS 行或缺行时整只排除。
- [x] 排除记录包含证券 IDs、异常范围、类型计数、capture 哈希和规则版本。
- [x] coverage 对排除证券以 explained missing 通过；未排除证券仍要求完整。
- [x] SPY/BIL、日历、费率和无法归属的坏数据不可排除。

## 阶段 5：2018 SEC 费率证据

- [x] `sync-fees` 增加 `--start/--end`，默认覆盖 2018-05-22 至当前日期。
- [x] 冻结 SEC 2018-67，验证 `2018-05-22` 与 `$13.00/million`。
- [x] 新 fee batch 纳入最终 lineage，2018-08-17 行绑定唯一 SHA-256。

## 阶段 6：测试

- [x] alias review、CBOE、GE、MIC、哈希/重叠拒绝测试。
- [x] 因果 availability、真正晚到、时间矛盾测试。
- [x] SCOPE-C 排除、基准不可排除、未排除缺行阻断测试。
- [x] 2018 fee active row 测试。
- [x] `py -3 -m unittest discover -s research_platform/tests -v`。
- [x] `py -3 -m unittest discover -s strategy_v1/tests -v`。

## 阶段 7：重建市场产物

- [x] assemble：`REVIEW_READY / blocking=0`。
- [x] CBOE 60 个决策月均有唯一 alias；GE alias 边界正确。
- [x] prepare-market：`MARKET_READY / blocking_gaps=0`。
- [x] 验证 alias、next-open、lineage、action evidence、fee、coverage、invalid OHLCV blocking 全部归零。
- [x] 保存最终 workspace、market batch、artifact hashes 和排除统计。

## 阶段 8：发布与资格

- [x] `us-pit build` 生成 `DATA_READY` release。
- [x] `us-pit validate` 无 Critical/High。
- [x] `us-pit qualify`；失败时不调整策略或门禁（结果：`HISTORICAL_FAILED`）。
- [x] 仅在资格通过后执行 `us-paper admit-release`；本次资格未通过，按门禁未执行，未启用真实交易。

## 阶段 9：文档与提交

- [x] 更新 `docs/roadmap.md` 和 `data/us_pit/action_reviews/CASE_NOTES.md`。
- [x] 在本文件记录最终命令与 IDs，未完成项不得勾选。
- [x] 不提交 tmp、TDX 二进制、市场数据、账户配置或大体积产物。
- [x] 提交并推送 main；最终 `git status` 干净。

## 最终执行记录（2026-08-30）

### 固定命令

```powershell
py -3 -m research_platform us-pit build --input-dir data\us_pit\market_reviewed\oxalpha_v41_market3 --source-batch 7b36185a2af41769090d64e636201956faac9cb684fd36a770f78fcd14fc0760
py -3 -m research_platform us-pit validate --release 62fb61d44c727f588c753a99a3811ebd6f16ec7c89692e0ad7df27945942d983
py -3 -m research_platform us-pit qualify --release 62fb61d44c727f588c753a99a3811ebd6f16ec7c89692e0ad7df27945942d983
py -3 -m unittest discover -s research_platform/tests -v
py -3 -m unittest discover -s strategy_v1/tests -v
```

### 不可变 IDs 与状态

- 权威 action source batch：`954089270e88e67b12d10fcc80156ac408d7c7a48d279fcc7258492677b81fb6`。
- alias review package：`661823fb8be0c66dd983e95074855c63a237f86c2ee51424522fd60a83ac9616`（14 行）。
- review workspace：`d698d86ada90b0000438629d5f837ca8c5f5989ecbf87a7820f44ac651f39345`，`REVIEW_READY`，blocking 0。
- market workspace：`oxalpha_v41_market3`；source batch `7b36185a2af41769090d64e636201956faac9cb684fd36a770f78fcd14fc0760`，`MARKET_READY`，blocking gaps 0。
- release：`62fb61d44c727f588c753a99a3811ebd6f16ec7c89692e0ad7df27945942d983`；manifest SHA-256 `bbfdc027b07e4ad0510275aea91483d43b3e60c20d8c5ff9ee2205af4c9a5abe`；build/validate 均 `DATA_READY`，Critical/High 0。
- Scope-C：197 个稳定证券、295 条排除记录；集合 SHA-256 `a42fdf45de42d65cdc6d73d9bd7c3d262fad16023c260ba560b9ac7729f7577a`。
- 资格 freeze SHA-256：`11636d236c362c9725d560a976f36576b2b9e05aac5b7b0019f45e20b7b72c00`；historical evidence SHA-256 `132f96c72f09fe81995c78eeee07a976fe8f205227286bd3e1a77c3ef8c62740`。
- 最终状态：`HISTORICAL_FAILED`。失败门槛：OOS CAGR vs BIL/SPY、OOS Sharpe absolute/vs SPY、OOS drawdown absolute/vs SPY、top issuer removed、sealed drawdown、double-cost CAGR/Sharpe、neighborhood pass count。
- paper release admission 未执行；admission count 0；`broker_writes_enabled=false`；`real_broker_order_entrypoints=false`；`execution_mode=PAPER_ONLY`。

### 主要市场工件

- `bars_raw`: 442596 行，SHA-256 `ae2d7c01d4e1169d5ec5d0b5727b2eacf247a1c59a221c141e7add34b384cf42`。
- `bars_pit_signal`: 12347329 行，SHA-256 `22447371fefac0b2764f4de3b310c5da262287f91bc8503c26bc4ec4180ade22`。
- `listing_aliases`: 630 行，SHA-256 `7b1aea6d18c7e9b35d8675c1e5a85e0890e64052875b05acf39bc519d46b3379`。
- `corporate_actions`: 34 行，SHA-256 `d92d1b650217022bde3e38594aad08120ecd05752568504b7b76e55222a815ec`。
- `execution_fee_schedule`: 13 行，SHA-256 `7e3b52cbbe2f2039075f255e7944a6e5e285b18d87dfff73892483daf050c9eb`。
