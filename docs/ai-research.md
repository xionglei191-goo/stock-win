# 大模型研究助手

## 目标与边界

研究助手把扫描、候选审批、模拟成交和事后反馈组织成可审计的日常闭环。大模型只解释平台已经计算出的结构化证据，不计算行情指标、不改变信号状态、不提交订单，也不决定策略晋级。

默认使用 OpenAI Responses API 和 `gpt-5.6-terra`。模型可由 `OPENAI_MODEL` 覆盖，密钥只从 `OPENAI_API_KEY` 读取。请求显式设置 `store=False`；数据库只保存模型 ID、响应 ID、提示词版本、输入哈希、token usage、结构化结果和错误，不保存密钥。

## 每日流程

1. 用户从 Web 或 `daily-research` CLI 显式发起扫描，默认策略为 `course49_system`，默认不推送 TDX。
2. 扫描继续执行既有数据健康门槛、点时截断、信号持久化和模拟组合处理。
3. 扫描成功后，研究助手从运行、信号、持仓、最近回测和数据健康状态构造裁剪后的确定性上下文。
4. 模型一次性生成日报和全部候选意见。意见只能为 `SUPPORT`、`OPPOSE` 或 `INSUFFICIENT`，不会自动批准或拒绝。
5. 用户审批时记录理由标签、置信度、最大可接受亏损、备注及是否与模型意见一致。
6. 后续 `refresh-feedback` 使用模拟成交或拒绝信号反事实刷新 1/3/5 日结果和决策画像。

## 证据与数据发送

每条模型结论必须引用当前输入中存在的 `run:*`、`signal:*`、`position:*`、`backtest:*` 或 `data_health:*` 证据 ID。不存在的引用、结构化输出校验失败或模型拒答都会使整份简报失败，不展示部分结果。

发送给模型的内容限于运行摘要、候选证据、持仓风险、回测指标和数据健康结果。不发送全市场原始 K 线、Parquet 文件、TDX 配置、API 密钥、账户身份信息或真实订单信息。模型失败不回滚扫描、信号、审批和模拟成交。

## 人工责任

候选审批始终由用户完成。TDX 回传必须显式勾选，默认关闭。研究助手输出是研究证据摘要，不是收益保证、投资建议或真实交易授权。

## 接口与命令

- `POST /api/research/daily`：扫描并生成日报。
- `POST /api/research/briefs`：基于既有运行生成日报。
- `GET /api/research/briefs` 与 `GET /api/research/briefs/{id}`：查询审计结果。
- `POST /api/research/feedback/refresh` 与 `GET /api/research/feedback/summary`：刷新和查询反馈。
- `py -3 -m research_platform daily-research`
- `py -3 -m research_platform generate-brief --run-id <RUN_ID>`
- `py -3 -m research_platform refresh-feedback`

## 失败降级

缺少密钥时，核心平台和数据健康状态不降级，研究助手接口返回明确的不可用原因。网络超时、限流、拒答和无效结构经过有限重试后记录为 `FAILED`；页面仍显示确定性运行和候选数据，不生成替代文本。
