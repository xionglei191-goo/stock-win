# 本地运维

## 启动顺序

1. 启动并登录 `D:\Project\stock\tdx-mock\TdxW.exe`。
2. 检查环境：`py -3 -m research_platform doctor`，正常结果为 `READY`。
3. 初次或板块变化后同步：`py -3 -m research_platform sync --refresh-sectors`。
4. 构建页面：在 `frontend` 执行 `pnpm install`、`pnpm build`。
5. 启动平台：`py -3 -m research_platform serve`，访问 `http://127.0.0.1:8000`。

## 日常流程

```powershell
# 只写本地研究库，建议作为默认操作
py -3 -m research_platform scan

# 明确需要客户端联动时才使用
py -3 -m research_platform scan --push-tdx

# 运行默认 49课体系回测
py -3 -m research_platform backtest --strategy course49_system

# 单独运行 49课体系；研究时可用 --playbooks 选择剧本
py -3 -m research_platform scan --strategies course49_system
py -3 -m research_platform backtest --strategy course49_system --playbooks recovery_ignition,ferment_second_board,acceleration_core_relay

# 查看缓存与执行 50GB 治理
py -3 -m research_platform cache-status
py -3 -m research_platform cache-prune

# 明确忽略快照并重新读取通达信
py -3 -m research_platform backtest --strategy course49_system --refresh-data

# 查看插件、数据需求和组合目录
py -3 -m research_platform catalog

# 复现含配对套利的历史资金分舱；该组合不再支持扫描
py -3 -m research_platform backtest --strategy adaptive_multi_strategy

# 重算配对套利五段冻结审计
py -3 -m research_platform pairs-arbitrage-study

# 重算或断点续跑缠论五段冻结审计
py -3 -m research_platform chan-study

# 扫描并生成结构化日报，默认不推送 TDX
py -3 -m research_platform daily-research

# 基于已有运行重新生成日报
py -3 -m research_platform generate-brief --run-id <RUN_ID>

# 刷新审批和反事实反馈
py -3 -m research_platform refresh-feedback

# V9 + 深沪单日逆回购：追加当天的纯观察影子账本
py -3 -m research_platform v9-repo-shadow

# 只读查看已积累交易日、外部证据缺口和观察门槛
py -3 -m research_platform v9-repo-shadow-status
```

`v9-repo-shadow` 与平台 `scan --mode paper` 完全隔离。它直接复用冻结的 `course49_v9/9.0.0` 计算规则，但只向 `v9_repo_shadow_events` 追加虚拟信号、次日开盘虚拟成交、持仓估值和逆回购结算；不会写 `signals`、`paper_orders`、`paper_positions`，不会解冻 V9 旧账户，也没有 TDX 推送或真实订单入口。首次观察边界严格晚于 `2026-08-07`；若漏过中间交易日，命令返回 `BLOCKED_FORWARD_GAP`，不会用事后数据回填。

完整采集运行在可终止的独立子进程中，默认上限 300 秒，可用 `--tdx-run-timeout` 调整；如本机 TDX 通常响应很快，还可用 `--tdx-probe-timeout 10` 启用一次可选的快速探测，未指定时不重复初始化 TDX。即使 TDX 初始化或关闭异常，主命令也会终止子进程并复核账本是否已完整提交，不会留下持续占用单通道的 Python 进程。

观察协议要求至少 60 个新交易日，但达到数量门槛也不会自动进入模拟盘。账户合同/交割单仍须证明逆回购佣金、次日开盘前本金可用性，并完成临近收盘可执行报价审计；在此之前状态保持 `COLLECTING` 或 `BLOCKED_EXTERNAL_EVIDENCE`，`paper_simulation_ready` 始终为 `false`。当前冻结协议哈希为 `3d32288d3c54594e1102f212f7b56a714b3abbbd54da73f8b264e38ced415c07`。

Web 的“49课”页展示共享上下文、六层漏斗、三个剧本、最终路由和持仓风险；“回测”页把体系、独立策略和研究归档分组，归档默认折叠。最多三个本地作业并行，重复参数使用 single-flight 返回同一任务；通达信读取仍由跨进程唯一通道保护，页面会显示等待原因、上下文/事件/剧本/路由阶段、快照命中和资源占用。扫描与回测均按策略 `DataPlan` 读取数据，固定股票套利不会加载全市场。49课页和候选页只审批体系最终信号；Chan V1 与配对套利 V1 已关闭扫描，只保留历史回测。所有做空与多腿成交仅进入模拟账户。

默认性能参数可通过 `RESEARCH_WORKER_THREADS`、`RESEARCH_MEMORY_CACHE_GB`、`RESEARCH_MIN_AVAILABLE_MEMORY_GB`、`RESEARCH_DISK_CACHE_GB` 和 `RESEARCH_BACKTEST_WORKERS` 调整。64GB/12线程机器建议保持默认 8 线程、24GB LRU、至少 8GB 可用内存和 3 个作业。

## 定时任务与样本外积累

平台运维由 Windows 计划任务驱动，全部只读、不下单：

- `ResearchPlatform-USPIT-ForwardCapture`：每 5 分钟检查，仅在真实 XNYS 会话收盘后 15 分钟采集 iShares/TDX/别名交叉验证证据；缺日不回填。状态用 `py -3 -m research_platform us-pit worker status` 与 `us-pit forward-status` 检查。
- `ResearchPlatform-DailyOps`：交易日 16:30 运行 `scripts/ops_daily.ps1`（`refresh-feedback`、`cash-instrument-status`）。
- `ResearchPlatform-WeeklyOps`：周六 09:00 运行 `scripts/ops_weekly.ps1`（`doctor`、`refresh-weekly-observations`、`us-pit forward-status`）。
- 日志追加在 `data/ops_logs/`；注册/重装脚本为 `scripts/register_ops_tasks.ps1`。

里程碑复验清单（结果只追加进 `docs/roadmap.md`）：

- 现金工具验证：累计 20 个纸面扫单日时出中期报告；60 个新交易日后才允许评估最终门禁。
- V4/V5/V6 奖励策略：样本量达标后重跑正式门禁；未达标前保持 `UNVERIFIED`。

## 独立校验门禁与数据覆盖审计

`py -3 -m research_platform validation-gates` 运行三个独立校验：官方交易日历质量索引冷重放、冻结复权因子源能力评估的 fail-closed 完整性、公司行动证据清单重放。已注册工件损坏或漂移时回测被阻断（退出码 2）；未注册只报告不阻断。引用文件在 `data/validation_gates/`。

`py -3 -m research_platform snapshot-coverage` 审计点时板块成分快照的前向积累（有效日期、质量分布与超过 7 个自然日的缺口）。龙虎榜席位明细库使用 `py -3 -m research_platform lhb-seat-store import|coverage`：内容寻址、按发布日期保存、只插入不更新；席位名称标准化待库存积累后另立计划。

## 故障定位

- `PARTIAL`：检查 TDX 是否运行、登录以及 `127.0.0.1:17709`。
- `BLOCKED_DATA`：查看数据页的覆盖率、新鲜度与错误信息；不要绕过质量门槛。
- 扫描请求包含 Chan 或 `combined`：两者已被历史审计关闭扫描；使用 `course49_system`，或只在回测页复现历史结果。
- Web 没有最新资源：重新执行 `pnpm build`，后端只服务 `frontend/dist`。
- 旧 Course49 策略看不到：它们已归档；在回测页开启“显示研究归档”后仍可直接回放。
- 自定义组合无法回测：信号融合、交集和风险覆盖当前为扫描模式；改用资金分舱，或先实现对应的逐日历史组合器。

当前构建固定为研究、严格回测和模拟盘，不挂载真实交易 API；TDX 券商写传输未编译，环境
变量不能重新开启下单或撤单。备份 `data/research.db` 与所需快照即可保留研究记录，但不要
把任何账户信息写入配置文件、数据库或 Git。

研究助手需要 `OPENAI_API_KEY`，可用 `OPENAI_MODEL` 覆盖默认 `gpt-5.6-terra`。缺少密钥不会阻断扫描、回测和模拟组合；只会使研究助手任务明确失败。审批和研究助手页面中的 TDX 推送默认关闭。

实验源码位于 `data/strategy_lab/`。可备份该目录保留本地实验，但不要把未验证源码复制到正式策略目录。实验子进程超时或内存超限时应检查实验详情，不得绕过校验直接晋级。
