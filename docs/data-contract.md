# 数据契约

## 不可变回放快照

正式回测只保存合并 `DataPlan` 要求的数据集，可能包含 `daily_front`、`daily_raw`、`style_benchmarks`、`market_index`、`security_master`、`sector_membership`、`market_activity`、`limit_behavior` 与 `dragon_tiger`。每个数据集在 SQLite 中登记文件路径、查询参数、行数和分块计算的 SHA-256；回放前必须重新计算哈希。股票名称与 ST 状态使用快照中的 `security_master`，旧快照缺失时允许读取当前通达信名称，但必须标记 `security_master_source=current_fallback`，该结果不得作为严格历史验证证据。

## 行情字段

策略接收以证券代码为键的 `pandas.DataFrame`：

| 字段 | 含义 | 标准 |
| --- | --- | --- |
| `Open/High/Low/Close` | OHLC | 人民币价格，浮点数 |
| `Volume` | 成交量 | 股 |
| `Amount` | 成交额 | 元 |
| 索引 | K 线结束时间 | `Asia/Shanghai` 语义，升序且唯一 |

前复权数据用于收益、均线和结构分析；不复权数据用于成交、涨跌停和模拟执行。每个 DataFrame 的 `attrs` 记录 `source`、`adjustment`、`amount_unit`、`volume_unit` 和 `timezone`。

## 龙虎榜事件

`dragon_tiger` 来自通达信 `get_gpjy_value`，保存总买卖、机构、营业部、北向、连续上榜和专业机构字段。原始金额单位为万元，适配层统一转为元，并使用事件日不复权成交额计算净买比例。事件以 `event_date` 为可用边界：策略日为 `T` 时只能读取 `event_date <= T` 的记录；没有记录表示“未上榜”，不填零流出。标准化事件随扫描或回测写入 `data/snapshots/<snapshot_id>/dragon_tiger.parquet`。

## 49课市场与涨停行为

`market_activity` 来自通达信 `get_scjy_value`，按交易日保存全市场涨跌家数、涨跌停、连板高度、回封率以及封板/炸板资金。`limit_behavior` 来自 `get_gpjy_value`，保存个股首封时间、开板次数、封单比例、竞价量、竞价涨停委买及历史封板/溢价统计。通达信金额字段由万元统一为元，百分数字段统一为 0 到 1；时间统一为 `HHMMSS`。

两个数据集均是收盘后可用的逐日事实。策略日为 `T` 时必须先执行 `index <= T` 切片；零值记录不能被解释为涨停或龙虎榜事件。标准化结果分别写入 `market_activity.parquet` 与 `limit_behavior.parquet`。

## 风格基准与板块成分

Course49 体系优先读取 `000300.CSI`、`000852.CSI` 和 `399006.SZ`，沪深300/中证1000允许 `.SH` 兼容回退。`benchmark_actual_codes` 必须记录实际代码；关键大小盘基准缺失时不得产生新仓。

同步、扫描和回测均写入 `sector_membership.parquet` 及内容哈希。回测优先使用交易日前最近快照；本地尚无历史快照时使用当前通达信成员，并标记 `sector_membership_quality=LIMITED`、`source=current_fallback`。该标记是长期题材回测的强制风险提示。

## 可用时间

任何信号必须满足 `available_at >= generated_at`，且只引用该时刻及之前的数据。日线策略在收盘后生成信号，最早在下一交易日开盘执行；30 分钟策略在 K 线收盘确认后，使用下一根可交易 K 线执行。

## 策略数据声明

每个策略通过 `DataRequirement` 声明 `dataset/frequency/adjustment/lookback/required/fields`。策略目录将声明与 `SourceRegistry` 关联，页面显示权威来源和是否可缓存。新增策略不得绕过登记表直接读取未注册数据；外部宏观、新闻或美股数据也必须先登记发布时间语义。

## 存储职责

- SQLite `data/research.db`：策略、运行状态、信号、人工决策、订单、成交、持仓、权益和回测摘要。
- Parquet `data/snapshots/`：研究运行实际使用的标准化行情、市场生态、涨停行为和龙虎榜事件快照。
- `snapshots` 表：快照路径、来源、创建时间和参数，建立运行到数据的追溯链。
- `strategy_frameworks/strategy_playbooks`：体系与剧本元数据、生命周期和数据需求。
- `backtest_states`：逐交易日市场周期、风格、适用度、模式和完整状态证据。
- `backtest_playbook_states`：每日准入、候选、路由、预算、阻断原因和漏斗。
- `snapshot_dependencies`：行情快照对独立事件片段的不可变引用和清理保护。
- `strategy_runtime_states`：V2 模拟持仓的连续弱势计数、最高收盘价和原题材。
- `order_group_intents/legs/decisions`：多腿研究意图、交易腿及人工审批审计。
- `paper_group_positions/fills`：多腿模拟持仓和原子成交，包含多空方向与入场费用。
- `research_briefs/ai_signal_reviews`：模型、提示词、输入哈希、结构化简报、候选意见及证据引用。
- `signal_decisions/decision_outcomes`：人工理由、置信度、模型一致性、实际模拟结果与拒绝信号反事实。
- `strategy_experiments/experiment_artifacts`：生成源码哈希、隔离校验、回测比较、压力结果和晋级记录。
- `backtest_trades.evidence/group_key/leg_id/framework_id/playbook_id/policy_version`：成交对应的不可变证据、多腿归属和剧本来源。
- `data_cache_entries`：缓存键、类型、快照、数据时点、覆盖范围、状态、大小和最近访问时间。Schema 版本为 V9。

## 模型上下文契约

模型输入由数据库记录构造为带证据 ID 的裁剪 JSON。原始全市场 K 线、快照文件、密钥、客户端配置和身份信息不得进入模型上下文。模型输出中的每个证据引用必须属于当次输入，验证失败时不保存为可展示结果。

反馈评价使用独立快照：批准项以模拟成交为实际口径，拒绝项只检查次一可交易日，不能向后寻找更有利开盘。1/3/5 日结果、费用、可交易性和评价时点必须与快照哈希一起保存。

## 数据质量

健康检查覆盖空数据、覆盖率、时间新鲜度、关键字段和指数基准。来源注册表标记 `READY`、`DEGRADED`、`STALE` 或 `UNAVAILABLE`。新增外部来源必须先注册字段、频率、时区和发布日期，不能直接在策略中临时抓取。
