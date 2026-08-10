# 回测规范

## 可复现验证

全量回测会额外保存 `security_master` 与 `market_index`，并记录数据、快照和策略执行的分阶段耗时。49 课压力测试必须从原回测的不可变快照回放；读取时校验 Parquet 内容哈希，禁止重新拉取后仍声称使用同一数据。

回测先根据全部策略的 `DataRequirement` 生成统一 `DataPlan`。显式历史区间会把截止日期传给通达信，读取量只覆盖区间和 90 根预热；固定股票套利只读取声明的股票。相同或覆盖区间快照在股票池、数据集、复权和事件覆盖兼容时自动复用，策略版本与交易成本不进入数据缓存键。页面勾选“重新读取通达信数据”或 CLI 使用 `--refresh-data` 可强制刷新。

```powershell
py -3 -m research_platform backtest-replay --source-backtest-id <ID> --execution-cost-multiplier 2
py -3 -m research_platform validate-course49 --baseline-backtest-id <ID> --stress-backtest-id <STRESS_ID> --historical-holdout-backtest-id <HOLDOUT_ID>
```

页面中的“2 倍成本回放”支持所有版本化 Course49 策略并执行相同流程。正式收益验证要求：完整历史至少 250 个交易日和 30 笔平仓；四段时间切片至少一半为正；同版本非重叠历史留出集至少 250 日、20 笔平仓且总收益为正；平仓收益中位数为正，剔除前三大赢家后仍为正；策略冻结日 `2026-08-09` 之后至少 60 个交易日、10 笔平仓且年化不低于 20%；2 倍佣金、税费和滑点回放保持正收益。冻结日前达到 20% 只能标记为 `HISTORICAL_RETURN_TARGET_MET`，不能称为已验证。

Python 是主回测引擎，确保策略逻辑、执行限制和研究数据库使用同一套实现。`combined` 运行 `chan_v1 + course49_system`；`course49_system_compare` 让旧 V2 与新体系共享快照；`adaptive_multi_strategy` 使用 35% Course49、25% 缠论和 40% 配对套利资金分舱。V1-V11 仅作为研究归档回放。

```powershell
py -3 -m research_platform backtest --strategy chan_v1 --daily-bars 180
py -3 -m research_platform backtest --strategy course49_v1 --start 2025-01-01 --end 2026-06-30
py -3 -m research_platform backtest --strategy course49_v2 --start 2025-01-01 --end 2026-06-30
py -3 -m research_platform backtest --strategy course49_system --start 2025-01-01 --end 2026-06-30
py -3 -m research_platform backtest --strategy course49_system --playbooks recovery_ignition,ferment_second_board
py -3 -m research_platform backtest --strategy course49_system_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_compare --sampling-mode stratified --max-stocks 500
py -3 -m research_platform backtest --strategy course49_v3_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v4_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v5_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v6_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v9_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v10_compare --sampling-mode full
py -3 -m research_platform backtest --strategy course49_v11_compare --sampling-mode full
py -3 -m research_platform backtest --strategy combined --universe growth --start 2025-08-01 --end 2026-08-01
py -3 -m research_platform backtest --strategy pairs_arbitrage_v1 --daily-bars 320
py -3 -m research_platform backtest --strategy adaptive_multi_strategy --daily-bars 320
py -3 -m research_platform backtest --strategy course49_v2 --start 2025-01-01 --end 2026-01-01 --refresh-data
py -3 -m research_platform backtest --strategy chan_v1 --universe custom --codes 600519.SH,000001.SZ
py -3 -m research_platform diagnose-course49 --backtest-id <BACKTEST_ID>
```

股票池支持全 A 股、沪深主板、创业板、科创板、北交所和自定义代码。默认 `sampling-mode=full`，全 A 不设 500 只上限。显式选择 `stratified` 才抽样；旧请求只要含 `--max-stocks` 会自动切换分层模式。抽样按沪主板、深主板、创业板、科创板、北交所及 20 日成交额三分位分层，默认种子 49，结果可复现。

所有 V2 回测日按当时可见数据排除 ST/退市、停牌、上市不足 60 根日线及 20 日平均成交额低于 2,000 万元的股票。股票池哈希、交易所/板块分布、实际基准代码、策略版本及板块成分可信度写入 `parameters_json`。

49课回测先从区间行情中筛出曾涨停的股票，再批量读取这些股票的历史涨停行为与龙虎榜，避免对全市场盲查；市场生态另按区间一次读取。快照记录最低连板覆盖数，首板策略不能回放仅覆盖二板以上的旧快照。事件标准化只保留真实龙虎榜或涨停日期，三类结果写入独立快照，每个回测日通过事件日截断读取，未来事件不会参与当日排序。历史板块成分仍使用当前通达信快照，因此长期题材归属研究必须标注这一限制。

回测结果写入 `backtests`、`backtest_equity`、`backtest_trades`、`backtest_states` 和 `backtest_playbook_states`。页面展示收益指标、权益曲线、逐笔交易、持仓变化、体系对比、市场风格时间线、剧本收益归因、候选漏斗、路由拒绝原因、资金利用率及实际股票池分布。`playbook_ids` 只影响研究回测；扫描始终使用固定生产剧本。

`diagnose-course49` 默认从完整 Parquet 快照重建市场阶段，输出 `data/research/course49_reward_<ID>_snapshot.json` 和逐事件 CSV；增加 `--scope backtest` 可严格限制到原回测状态窗口。报告同时保留事件研究的持有日收盘收益，并以“观察 N 个持有日后下一开盘退出”的 `execution_net_return_*` 作为摘要口径，与平台实际成交模型一致。它独立于组合回测，用于定位市场阶段、连板高度和涨停制度的条件收益；事件重叠、持仓上限和资金占用仍以正式回测为准。

配对套利使用收盘生成意图、下一交易日两腿同时开盘成交，任何一腿缺失或涨跌停阻断都会取消整组当日成交。权益按 `现金 + 多头市值 - 空头市值` 计算，并展示总敞口、净敞口、完成组合胜率和组合盈亏。做空仅是研究模拟，不代表通达信或实际账户具备融券资格。

## 可信性要求

- 日线信号下一交易日开盘成交。
- 复权数据生成特征，不复权数据处理成交和涨跌停。
- 同一参数集必须绑定同一数据快照；不得用运行结束后补全的数据覆盖证据。
- 人工审批策略在历史回测中采用规则化自动批准，并在结果中标明这一假设。
- 新策略必须增加防未来函数、执行约束和边界数据测试。
- 非资金分舱组合目前只支持扫描，后端拒绝以独立曲线相加伪装成融合回测。
- 当前板块成员来自通达信当前快照；跨多年题材回测需警惕成分变化造成的幸存者偏差。
- 事件研究只能用于发现候选规则；策略晋级必须使用未参与规则选择的样本外区间，并报告交易数、资金利用率、回撤和成本敏感性。

通达信 Lambda 后续只用于客户端侧快速验证与结果对照，不取代主回测记录。

## 性能与并发

默认使用 8 个本地处理线程、24GB 内存 LRU 和 50GB 磁盘上限。行情批量起始为 800 只、专业事件为 500 只，空结果自动减半到 100 只；通达信请求永远单通道。本地服务最多同时运行三个作业，快照回放可以真正并行，冷任务只在取得 TQ 通道前等待。Course49 上下文、市场、资格、板块、龙头和事件特征按“快照哈希 + 上下文版本”缓存，对比实验和三个剧本只计算一次。

长任务按行情类型和事件批次上报已完成股票数。2026-08-10 本机验收中，`2026-07-01` 至 `2026-08-07` 的全 A Course49 V2（5,550 只原始股票、5,103 只合格股票）冷运行耗时 359.2 秒、峰值内存约 869MB；磁盘快照命中 64.3 秒，进程内命中 37.4 秒。三档均低于 28 日验收上限。

缓存快照先以 `BUILDING` 状态写入，所有数据和策略执行成功后才原子发布为 `READY`。失败项不会命中；治理顺序为失败项、可重建特征、未引用数据。被回测或运行引用的证据快照及其 `snapshot_dependencies` 事件片段不自动删除。

## 自动实验回放

实验室仅接受已完成且拥有不可变快照的 `course49_v3` 基准。候选变体和双倍成本版本从同一快照回放，记录实验 ID、生成源码哈希和基准关联；禁止为候选重新拉取更有利数据。

代码与比较门槛通过只表示候选可进入“仅回测”目录，不等同于正式收益验证。250 日、30 笔平仓、分段、冻结后样本外和成本压力要求仍全部适用，未满足时必须显示 `UNVERIFIED`。
