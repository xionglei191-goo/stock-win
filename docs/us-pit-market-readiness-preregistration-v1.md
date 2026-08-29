# 预注册：美股 PIT 市场就绪规则（v1）

状态：已冻结（执行计划批准后落盘）。本文件必须先于实现提交；任何规则变化须另立新版本。

## 背景

审查工作区已达到 `REVIEW_READY`，但首次完整 `prepare-market` 仍存在 CBOE alias、GE share-ratio predecessor、公司行动证据可用时间、TDX 必需历史坏行和 2018 SEC 费率原始证据缺口。本规则只处理这些市场阶段缺口，不修改已冻结的成员重放、分拆残差、N-PORT supersede 或身份批准规则。

## R1 LISTING-ALIAS-REVIEW-v1

1. `listing_aliases` 的人工补充必须来自哈希绑定的批准记录，不得直接改 Parquet。
2. 每条记录必须包含稳定 security ID、ticker、vendor code、真实交易所 MIC、有效区间、证据 source ID/SHA-256、批准状态和 review note。
3. assembler 只接纳 `approved=true`、证据 CAS 对象存在且哈希一致的记录；冲突、重叠或未批准记录均 fail-closed。
4. CBOE 必须使用 `CBOE.US` 和真实 Cboe BZX MIC `BATS`，不得伪写为 Nasdaq。
5. exchange canonicalization 固定为：NYSE→XNYS、Nasdaq→XNAS、NYSE Arca→ARCX、Cboe BZX→BATS。产品决策日仍统一采用冻结 XNYS 日历。

## R2 ACTION-LINEAGE-ALIAS-v1

批准的 share-ratio predecessor/successor 行动可以支持同一 vendor ticker 的历史 alias 连续性，条件为：

- 行动已批准、`terms_verified=true`，存在唯一冻结证据依赖；
- predecessor alias 截止于生效前一 XNYS 交易日，successor alias 从生效日开始；
- 两段不得重叠；
- 补充记录绑定 action ID、证据 SHA-256 和人工批准；
- 不允许从名称或当前 ticker 猜测 predecessor。

本规则用于 GE reverse split：`us_isin_us3696041033 → us_isin_us3696043013`，vendor code `GE.US`。

## R3 CAUSAL-ACTION-AVAILABILITY-v1

1. source metadata 明确 `publication_time_from_payload=true` 且发布时间字段可解析时，历史可用时间取 `published_at`；本地后续抓取时间不抹去已由官方 payload 证明的历史公开事实。
2. 未验证发布时间时，可用时间取 `max(published_at, observed_at)`；任一时间缺失或可用时间晚于决策收盘均阻断。
3. 处理顺序固定为：唯一证据依赖 → 时间字段可解析 → `announced_at <= effective_at` → 条款已批准 → 可用时间不晚于决策收盘。
4. `announced_at > effective_at` 永远是结构性错误，不得以“决策时未知”跳过。
5. 不允许简单删除 `CORPORATE_ACTION_EVIDENCE_LATE` 或用 `continue` 把真正晚到证据变为非阻断。

## R4 SCOPE-C-QUALITY-v1

用户选择延续 SCOPE-C 并采用“质量范围排除”，不引入外部行情源。

1. warmup 是策略必需历史；回测开始日前的坏行不得仅因日期较早而豁免。
2. 对每个 TDX vendor code，在 alias 有效区间与完整必需 fetch window 的交集中检查：
   - OHLCV 有限、正值且 high/low 关系有效；
   - `(vendor_code, session)` 唯一；
   - 日期属于冻结 XNYS 会话；
   - 每个必需会话均有 raw/front 行，除非存在已批准的证券级 `HALTED/NO_TRADE` 例外。
3. 发现任一未解释问题时，该 vendor code 对应的全部 stable security IDs 整体退出本次 SCOPE-C 可交易样本；不得局部填充或合成价格。
4. 排除必须记录 vendor code、security IDs、首末问题日、问题类型计数、原始 capture 哈希和规则版本。
5. coverage 对正式排除证券将缺失计为 explained；未排除证券仍要求 raw/signal/next-open 完整。
6. SPY、BIL、日历、费率、alias 冲突及无法归属 vendor code 的坏数据不可排除，继续阻断。

## R5 REGULATORY-FEE-RANGE-v1

1. 费率证据同步范围必须覆盖市场 fetch 起点，而不是只覆盖决策起点。
2. 本产品最早支持日期固定为 2018-05-22；2018-08-17 active fee row 必须绑定 SEC 2018-67 冻结对象，逐字证明 `$13.00 per million` 和生效日。
3. 本地常量只能用于确定性派生，不能替代原始官方证据。

## 验收

- assemble：`REVIEW_READY`，hard blocking gaps=0。
- prepare-market：`MARKET_READY`，blocking gaps=0。
- CBOE 有唯一 `CBOE.US/BATS` alias；GE predecessor/successor lineage 连续且不重叠。
- 真正晚到/矛盾公司行动仍阻断；仅已验证历史发布时间消除假阳性。
- 坏行情只出现在正式 SCOPE-C exclusion 审计中，不出现在可交易数据中。
- 2018 SEC fee row 绑定唯一官方 SHA-256。
