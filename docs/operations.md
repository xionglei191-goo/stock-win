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

# 运行组合回测
py -3 -m research_platform backtest --strategy combined

# 单独运行 49课体系；研究时可用 --playbooks 选择剧本
py -3 -m research_platform scan --strategies course49_system
py -3 -m research_platform backtest --strategy course49_system --playbooks recovery_ignition,ferment_second_board,acceleration_core_relay

# 查看缓存与执行 50GB 治理
py -3 -m research_platform cache-status
py -3 -m research_platform cache-prune

# 明确忽略快照并重新读取通达信
py -3 -m research_platform backtest --strategy combined --refresh-data

# 查看插件、数据需求和组合目录
py -3 -m research_platform catalog

# 运行含配对套利的资金分舱组合
py -3 -m research_platform scan --strategies adaptive_multi_strategy

# 扫描并生成结构化日报，默认不推送 TDX
py -3 -m research_platform daily-research

# 基于已有运行重新生成日报
py -3 -m research_platform generate-brief --run-id <RUN_ID>

# 刷新审批和反事实反馈
py -3 -m research_platform refresh-feedback
```

Web 的“49课”页展示共享上下文、六层漏斗、三个剧本、最终路由和持仓风险；“回测”页把体系、独立策略和研究归档分组，归档默认折叠。最多三个本地作业并行，重复参数使用 single-flight 返回同一任务；通达信读取仍由跨进程唯一通道保护，页面会显示等待原因、上下文/事件/剧本/路由阶段、快照命中和资源占用。扫描与回测均按策略 `DataPlan` 读取数据，固定股票套利不会加载全市场。49课页和候选页只审批体系最终信号；配对套利必须整组审批。所有做空与多腿成交仅进入模拟账户。

默认性能参数可通过 `RESEARCH_WORKER_THREADS`、`RESEARCH_MEMORY_CACHE_GB`、`RESEARCH_MIN_AVAILABLE_MEMORY_GB`、`RESEARCH_DISK_CACHE_GB` 和 `RESEARCH_BACKTEST_WORKERS` 调整。64GB/12线程机器建议保持默认 8 线程、24GB LRU、至少 8GB 可用内存和 3 个作业。

## 故障定位

- `PARTIAL`：检查 TDX 是否运行、登录以及 `127.0.0.1:17709`。
- `BLOCKED_DATA`：查看数据页的覆盖率、新鲜度与错误信息；不要绕过质量门槛。
- Chan 被阻断但 49 课正常：通常是 30 分钟缓存过期，可在 TDX 更新分钟数据后重扫。
- Web 没有最新资源：重新执行 `pnpm build`，后端只服务 `frontend/dist`。
- 旧 Course49 策略看不到：它们已归档；在回测页开启“显示研究归档”后仍可直接回放。
- 自定义组合无法回测：信号融合、交集和风险覆盖当前为扫描模式；改用资金分舱，或先实现对应的逐日历史组合器。

平台不包含自动调度、外部新闻/Fed 数据、账号鉴权和真实下单。备份 `data/research.db` 与所需快照即可保留研究记录。

研究助手需要 `OPENAI_API_KEY`，可用 `OPENAI_MODEL` 覆盖默认 `gpt-5.6-terra`。缺少密钥不会阻断扫描、回测和模拟组合；只会使研究助手任务明确失败。审批和研究助手页面中的 TDX 推送默认关闭。

实验源码位于 `data/strategy_lab/`。可备份该目录保留本地实验，但不要把未验证源码复制到正式策略目录。实验子进程超时或内存超限时应检查实验详情，不得绕过校验直接晋级。
