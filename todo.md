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
- [ ] 新建并冻结 `docs/us-pit-market-readiness-preregistration-v1.md`。
- [ ] 删除 warmup 坏行直接过滤实验。
- [ ] 撤销公司行动简单 `continue` 实验，改用统一因果可用时间。

## 阶段 2：可复现 listing alias 审查链

- [ ] 增加哈希绑定、人工批准的 alias review 输入。
- [ ] 为 CBOE 冻结官方证据并生成唯一 `CBOE.US/BATS` alias。
- [ ] 为 GE reverse-split predecessor/successor 生成不重叠 `GE.US` action-lineage alias。
- [ ] 废弃临时 `oxalpha_final_v8c` 手工改写，基于权威 review 输入重建。
- [ ] exchange canonicalization 使用真实 MIC：`XNYS/XNAS/ARCX/BATS`。
- [ ] 安全处理 `pd.NA` 和 `ticker_review/exchange_review` fallback。

## 阶段 3：公司行动因果时间

- [ ] 抽取共享 source availability 帮助函数。
- [ ] 验证唯一依赖、时间、条款与发布时间证明。
- [ ] payload 验证发布时间时允许事后抓取证明历史公开事实。
- [ ] 未验证或真正晚到的证据继续 blocking。
- [ ] `announced_at > effective_at` 始终 blocking。

## 阶段 4：SCOPE-C 行情质量排除

- [ ] 按 vendor code 审计完整必需区间 raw/front 数据。
- [ ] 未解释坏行、重复行、非 XNYS 行或缺行时整只排除。
- [ ] 排除记录包含证券 IDs、异常范围、类型计数、capture 哈希和规则版本。
- [ ] coverage 对排除证券以 explained missing 通过；未排除证券仍要求完整。
- [ ] SPY/BIL、日历、费率和无法归属的坏数据不可排除。

## 阶段 5：2018 SEC 费率证据

- [ ] `sync-fees` 增加 `--start/--end`，默认覆盖 2018-05-22 至当前日期。
- [ ] 冻结 SEC 2018-67，验证 `2018-05-22` 与 `$13.00/million`。
- [ ] 新 fee batch 纳入最终 lineage，2018-08-17 行绑定唯一 SHA-256。

## 阶段 6：测试

- [ ] alias review、CBOE、GE、MIC、哈希/重叠拒绝测试。
- [ ] 因果 availability、真正晚到、时间矛盾测试。
- [ ] SCOPE-C 排除、基准不可排除、未排除缺行阻断测试。
- [ ] 2018 fee active row 测试。
- [ ] `py -3 -m unittest discover -s research_platform/tests -v`。
- [ ] `py -3 -m unittest discover -s strategy_v1/tests -v`。

## 阶段 7：重建市场产物

- [ ] assemble：`REVIEW_READY / blocking=0`。
- [ ] CBOE 60 个决策月均有唯一 alias；GE alias 边界正确。
- [ ] prepare-market：`MARKET_READY / blocking_gaps=0`。
- [ ] 验证 alias、next-open、lineage、action evidence、fee、coverage、invalid OHLCV blocking 全部归零。
- [ ] 保存最终 workspace、market batch、artifact hashes 和排除统计。

## 阶段 8：发布与资格

- [ ] `us-pit build` 生成 `DATA_READY` release。
- [ ] `us-pit validate` 无 Critical/High。
- [ ] `us-pit qualify`；失败时不调整策略或门禁。
- [ ] 仅在资格通过后执行 `us-paper admit-release`，不启用真实交易。

## 阶段 9：文档与提交

- [ ] 更新 `docs/roadmap.md` 和 `data/us_pit/action_reviews/CASE_NOTES.md`。
- [ ] 在本文件记录最终命令与 IDs，未完成项不得勾选。
- [ ] 不提交 tmp、TDX 二进制、市场数据、账户配置或大体积产物。
- [ ] 提交并推送 main；最终 `git status` 干净。
