# 预注册：分拆残差与指数前更名的成员重放规则（v1）

状态：已冻结（2026-08-25，操作员确认）。本文件一经提交不再修改；
后续任何变更必须另立新版本并重新走完整验证链。

## 背景事实（由冻结证据确立）

1. 锚点是 IVV 基金的真实 N-PORT 持仓，不是纯成员名单。指数基金在分拆后
   会按比例收到 successor 股份并短暂持有，随后卖出。
2. Sandisk 官方 ADD 生效日为 2025-11-28（冻结 S&P 页面逐字核验），
   因此其在 2025-03 至 2025-10 期间不是成员；3 月锚点中的头寸是分拆残差。
3. Under Armour 两类股官方 REMOVE 生效日为 2022-06-21；6 月末锚点中
   的残余头寸同属残差。
4. Embecta、Ralliant 从未有任何官方 ADD 事件；锚点中的头寸全部为
   BDX / FTV 分拆残差。
5. Gardner Denver Holdings 于 2020-03-03 经官方公告加入指数，其后更名为
   Ingersoll Rand Inc；GDI 不在归一化身份覆盖窗口内，现有操作员过渡
   机制无法表达该延续。

## 冻结规则

### R1 基线去污（成员集保持纯粹）

在任意时点 t，证券 S 满足以下全部条件时，判定为"分拆残差"，从
OBSERVED_SIGNAL 基线种子和决策月成员集中剔除：

- 存在一条已批准的 `SPINOFF` 公司行动，其 `successor_security_id == S`
  且生效日 ≤ t；
- 不存在 S 的官方 `ADD` 成员事件生效日 ≤ t。

剔除只影响基线/决策构造；不修改任何事件或行动记录。
当 S 此后出现官方 ADD 事件且生效日到达时，ADD 正常将其纳入成员集。

### R2 锚点对账豁免

相邻 SEC 锚点对账时，差异集合（missing 与 extra）中每一只证券若在
跟随锚点日满足 R1 的分拆残差判定，则记为"已解释差异"：

- 已解释差异不触发 `QUARTERLY_ANCHOR_RECONCILIATION_FAILED`；
- extra 方向的已解释残差不进入向后携带的重放状态（成员集保持纯粹）；
- missing 方向的已解释残差不被补入重放状态；
- 每条豁免必须能追溯到具体的 SPINOFF `action_id`，在缺口报告中
  以 `explained_by_spinoff_actions` 字段留痕。

无任何未解释差异且无事件冲突时，锚点对判为通过。

### R3 事件锚定的前身身份（指数前更名）

操作员过渡提议的前身稳定 ID 允许引用一条**已批准成员事件**中的
`security_id`（即使该 ID 不在归一化身份表中），条件：

- 提议必须显式携带来源事件的 `event_id`；
- 构建证据请求时必须验证：冻结的 S&P 公告原文同时包含前身与后继的
  官方名称（逐字命中），且来源事件存在于已批准审查输入中；
- 行动条款仍必须经既有 SEC 文件行动审查链逐字核验后方可批准。

不放松任何哈希校验；只是把前身锚点从归一化表扩展为
"归一化表 ∪ 已批准成员事件"。

## 明确禁止

- 禁止将分拆 successor 写入其官方 ADD 生效日之前的任何月份成员集；
- 禁止在无冻结公告原文双名称命中的情况下批准事件锚定过渡；
- 禁止以操作员备注代替上述任一机器可验条件。

---

## 实施后发现的两个遗留决策点（2026-08-25，待操作员批准）

R1/R2/R3 已实现并通过测试（membership_replay.py、evidence_requests.py）。
第四、五轮审查执行中发现规则仍无法闭合最后 5 个锚点日，原因与所需授权如下：

### D1 成员事件身份的动作锚定（对应 GDI ADD 阻断）

GDI 的官方 ADD（2020-03-03，冻结公告 51273ecb…）在成员事件审查中被
`CANDIDATE_IDENTITY_UNRESOLVED` 阻断——S&P 公告只有公司名与代码 GDI，
无稳定 ID，而归一化表不覆盖该时期。
现已存在经批准的公司行动（approval 09887be9…）：REORGANIZATION
us_isin_us36467w1099 → us_isin_us45687v1061 @2020-03-03，SEC 文件同时含
"Ingersoll Rand Inc. (formerly Gardner Denver Holdings, Inc., \"GDI\")"。

**需要的授权**：允许成员事件审查把未解析代码的事件绑定到"已批准公司行动的
predecessor 稳定 ID"，条件是该行动的证据摘录包含事件的 ticker 与双方名称
（本例全部满足）。批准后该 ADD 以旧 ID 入库，重放中同日继承到 New IR。

### D2 分拆行动的成本分摊比例证据来源

SPINOFF 行动校验要求 cost_basis_fraction ∈ [0,1)，但三起分拆
（Embecta/Sandisk/Ralliant）的税务成本分摊比例只存在于发行人分配后发布的
Form 8937（公司官网），SEC 文件明确写明"将于分配后在网站公布"
（Sandisk 10-12B/A 原文）。已核验的分拆比例：
Embecta 1-per-5（ratio 0.2）、Sandisk 80.1% 按比例分配（WDC 保留 19.9%）、
Ralliant 比例待从 Form 10 精确摘录。

**需要的授权**：新增"发行人官方非 SEC 文件"（Form 8937 / 信息声明补遗）
作为 cost_basis_fraction 的可接受证据类，经 import-evidence 冻结 +
人工审查引用；或授权 SPINOFF 行动在成员重放语境下免填 cost_basis
（交易语境仍要求完整条款）。

在 D1/D2 获得批准前，2020-03-31、2022-06/09、2025-03/06/09 五个锚点日
维持 DATA_BLOCKED 属实且诚实。

---

## 决策与实施结果（2026-08-25，操作员批准 D1+D2）

- **D2 已按方案 B 实施**：`_validate_terms` 允许 SPINOFF 免填 cost_basis
  （成员重放语境）；新增 `validate_execution_action_terms` 强制交易语境
  必须有 cost_basis_fraction。回归测试通过。
- **D1 已实施**：`scripts/admit_action_anchored_membership_events.py`
  按哈希校验链把被阻断的 GDI ADD（event 7bdbf451…）绑定到已批准行动
  （approval f0351634…）的 predecessor 稳定 ID us_isin_us36467w1099，
  admission_id bbb42c71…。
- 另发现并更正 r4 生效时区：2020-03-03 为 EST（-05:00），继承必须在
  GDI ADD 之后同日生效。
- 第五轮三条 SPINOFF 行动批准（approval 333a6c0e…）：Embecta ratio 0.2、
  Sandisk ratio 1/3、Ralliant ratio 1/3，均免填 cost basis。

### 最终装配 oxalpha_v12（workspace 795dc409…）

- QUARTERLY_ANCHOR_RECONCILIATION_FAILED = **0**（此前 14）
- 剩余缺口均为既有诚实状态：ISSUER_IDENTITY(652)、
  ANCHOR_RECONCILIATION_WINDOW_INVALID(1，最新锚点对窗口诊断)、
  EMPTY_REQUIRED_ARTIFACT(8，prepare-market 阶段)。

---

## 后续诊断（2026-08-25，prepare-market 阶段）

D1/D2 实施后装配 oxalpha_v14 验证：
- 季度锚点对账维持 **0 失败**
- ISSUER_IDENTITY(652) 未变，根因已查明：oxalpha 工作区 43,812 行持仓
  **全部为 VALIDATION_ANCHOR**，无任何 SIGNAL_INPUT 持仓。协议规定锚点不得
  回填发行人身份，而工作区内不存在可确立发行人身份的非锚点证据，
  因此该缺口是当前证据集的结构性诚实状态。
- prepare-market 正确拒绝在受阻工作区上运行 TDX 采集
  （REVIEW_WORKSPACE_BLOCKED_BEFORE_TDX），8 个市场工件保持为空。

### D3 待决策：发行人证据源接入

建议路径：引入 GLEIF 官方 ISIN→LEI 映射作为非锚点发行人证据
（import-evidence 冻结 + 逐行审查批准），使 `_issuer_id` 可经 LEI 解析。
备选：SEC 公司索引提供 CIK（但与 ISIN 的桥接仍需第三方映射）。

在 D3 批准前，DATA_BLOCKED 属实；prepare-market/build 无法诚实推进。

---

## D3 执行记录与 D4 残余（2026-08-26）

### 已实施
- **SEC 官方索引绑定**（`scripts/build_us_pit_issuer_evidence.py`）：
  两级规范化名称唯一匹配，同稳定 ID 强制同一发行人；652→93。
- **身份行批量批准**（`scripts/approve_us_pit_identity_rows.py`）：五项前置条件。
- **EDGAR 提议-验证绑定**（`scripts/bind_us_pit_exited_issuers_by_cik.py`）：
  操作员提议 CIK → EDGAR 官方档案注册名机器验证（canon/compact/全字母串/
  词序无关四重比较，支持 /NEW/ 类州后缀与已批准行动的曾用名链）→ 通过才入库；
  错误提议自动拒绝。93→35 只证券。

### GLEIF 结论
GLEIF ISIN→LEI 每日映射文件只覆盖新发行 ISIN，不含存量美股大市值公司，
不适用于本缺口；数据本身免费开放（CC-BY 4.0），未来若需 LEI 可用其 API。

### D4 残余：35 只证券（全部为已退出指数公司）
构成：约 12 只为 EDGAR 限流暂时失败（可冷却后重跑 binder 增量补齐，
机制已支持 .prev 增量）；其余为错误 CIK 提议、无行动关联的改名
（Fortune Brands→Innovations 等）、以及 FTS 全文检索噪声导致的
名称匹配未决。需要更精确的官方 name→CIK 工具或逐案文件证据后再闭合。
