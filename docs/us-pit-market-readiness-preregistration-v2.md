# 预注册：美股 PIT 市场就绪补充规则（v2）

状态：已冻结。本文只补充 v1 未定义的分拆成本分配与本地 TDX 生命周期证明；
不修改 v1 的 alias、公司行动可用时间、SCOPE-C 或费率规则。

## R6 CAUSAL-SPINOFF-BASIS-v2

当已批准的 `SPINOFF` 在历史决策日尚无可因果使用的发行人税务成本比例时，
允许使用分拆后首个父、子证券共同正常交易的冻结 XNYS 会话计算市场价值分配：

1. 分拆比例、父/子稳定 security ID、ticker、交易所和生效时间必须先由已批准的
   官方公司行动及 listing alias 证明。
2. 父、子证券必须来自同一次只读 TDX raw capture；`fill_data=false`，不得使用复权、
   前向填充、插值或外部行情。
3. 每只证券的 VWAP 固定为 `Amount * 10000 / Volume`。`Amount`、`Volume` 必须有限且
   严格为正；两行日期必须相同且为生效日当日或之后的首个共同 XNYS 会话。
4. 子证券成本比例固定为
   `ratio * child_vwap / (parent_vwap + ratio * child_vwap)`；结果必须严格位于 `(0, 1)`。
5. 该比例的可用时间是共同会话的冻结市场收盘时刻。早于该时刻的决策不得使用；
   若共同会话、任一原始字段、alias 或捕获哈希缺失，继续 fail-closed。
6. 发布的公司行动行必须记录方法版本、可用时间、父/子 vendor code、两只 VWAP、
   raw capture SHA-256 与哈希绑定的推导证据。事后 Form 8937 仅作交叉核验，
   不能倒填历史可用时间。

## R7 TDX-LIFECYCLE-SURVEILLANCE-v2

1. 最终 SCOPE-C 非终止证券的生命周期状态，可由本次只读 TDX 美股证券池 capture
   证明到研究窗口末日，前提是每个稳定 security ID 恰好映射到一个当前有效 alias，
   且其 vendor code 逐字存在于冻结池 payload。
2. 生命周期证明记录必须绑定证券池 CAS SHA-256、逐证券 vendor code、名称、
   当前有效 alias 和覆盖截止日；缺失或歧义继续阻断。
3. `CASH_MERGER`、`STOCK_MERGER`、`DELISTING`、`BANKRUPTCY` 的前身证券仍必须由
   已批准公司行动逐证券对账，不得用当前证券池替代。
4. `SPINOFF` 不终止父证券；父证券继续使用状态巡检，不得把分拆误判为终止行动。
5. 因 TDX 必需历史问题被正式 SCOPE-C 排除的证券不进入最终成员集，也不计入最终
   生命周期覆盖分母；排除集合仍须由 v1 的精确哈希门禁验证。

## 验收

- 分拆推导比例仅从同日 raw capture 生成，并在该日收盘前不可用。
- BDX/EMBC 的推导结果与发行人后续 Form 8937 数值在合理舍入范围内一致；
  后续文件不作为历史 signal input。
- 每个最终历史成员恰有一条有效生命周期行；非终止成员由 TDX 池与当前 alias
  联合证明，终止成员由唯一公司行动证明。
