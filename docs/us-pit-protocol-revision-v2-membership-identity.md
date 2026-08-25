# US PIT 协议修订 V2：SEC 申报身份交叉验证（已冻结）

- 状态：`FROZEN`（2026-08-25）
- 影响面：`us-pit review-membership-events` 的成员事件身份审批路径
- 实现：`research_platform/us_pit/spglobal_events.py`（`SPGLOBAL_EVENT_EVIDENCE_REVIEW_VERSION_V2`）
- 审查产物：`data/us_pit/reviews/membership_events_201910_202607_oxalpha_v2`

## 1. 动机

V1 协议要求名称匹配的方向性身份锚必须有独立官方 ticker 交叉验证，且验证源限定为
iShares 月末持仓。iShares 历史归档最早仅到 2021-08-31，导致 2020-01 至 2021-08 生效的
24 条 S&P 500 成员事件（WCG/XEC/AGN/CPRI/HP/COTY/HRB/KSS/ETFC/VNT/NBL/AIV/TIF/CXO/
FTI/FLS/SLG/XRX/VAR/FLIR/HFC/ALXN/MXIM）无法在证据完整的前提下获批。SEC N-PORT 虽
覆盖更早期间但不携带 ticker 字段。

## 2. 新基准（冻结）

新增审查期批准路径 `CODEX_DIRECT_EVIDENCE_REVIEW_V2`：名称匹配的方向锚
（`EXACT_NORMALIZED_SEC_ISSUER_DIRECTIONAL_ANCHOR`、状态 `REVIEW_REQUIRED`）在同时满足
以下全部条件时获批：

1. 冻结的 SEC-filed 身份交叉验证包（`us-pit-sec-identity-crosscheck-v1`）中存在
   `event_id` 精确匹配、`review_outcome=RESOLVED` 的行；
2. 其 `resolved_security_id` 与候选的 `suggested_security_id` 完全一致；
3. 其 ticker 与公告 ticker 一致；
4. 引用的申报文件对象（`evidence_sha256`）从 CAS 字节级精确重放；
5. 同一稳定标识符（ISIN/CUSIP 键）出现在捕获的 `sec_nport_ivv` 持仓身份候选中，
   且内容寻址谱系完整——即**两个相互独立的官方来源在同一稳定 ID 上交汇**：
   基金自身的 N-PORT 持仓 + 第三方机构的 N-PORT/N-PX 申报文件。

任何一条不满足即维持阻断。交叉验证包整体损坏（任一 RESOLVED 行的证据重放失败）
则整个输入被拒绝，不允许部分采纳。

## 3. 明确的边界（不改变的部分）

- 交叉验证只承认 **ticker↔稳定 ID 绑定**，不产生事件时点、成员状态或信号可用性；
  事件时点仍由 S&P 公告的 `announced_at < effective_at` 管辖。
- 不回填历史信号可用性（`late_filing_never_backdates_signal_availability` 保持）。
- 六条真正股级歧义案例（VIAB、STI、GDI、NKTR、MBC×2）继续阻断——这是正确行为。
- 下游装配器（assemble-reviewed）与构建门禁不受影响；V1 无交叉验证输入时行为与
  输出格式完全不变（版本号保持 v1，仅在提供输入时升级 v2 并记录
  `identity_crosscheck_input` 溯源块）。

## 4. 执行结果

- 输入：候选集 `spglobal_201910_202607_v11`（221 条）、归一化
  `a1253b39…`、S&P 事件批次 `11334161…`+`8013e995…`、交叉验证包
  `sec_identity_crosschecks_201910_202607_codex_v2`
- 结果：**215 批准 / 6 阻断**（V1 基线：191/30），24 条经由双官方来源规则获批
- 审查者：`ox-alpha-evidence-review`（受数据所有者委托执行确定性机器验证）

## 5. 测试

`research_platform/tests/test_us_pit_membership_review_v2.py`：
正例准入、篡改证据整包拒绝、稳定 ID 不匹配保持阻断、BLOCKED 行永不准入。
