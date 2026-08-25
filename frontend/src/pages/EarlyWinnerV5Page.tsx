import { useQuery } from '@tanstack/react-query'
import { DatabaseZap, FileLock2, LockKeyhole } from 'lucide-react'
import { api } from '../api'
import { ErrorState, LoadingState, PageHeader, StatusBadge } from '../components'

export default function EarlyWinnerV5Page() {
  const project = useQuery({
    queryKey: ['early-winner-v5'],
    queryFn: api.earlyWinnerV5,
    refetchInterval: 10_000,
  })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />

  const data = project.data!
  const master = data.data_gates.historical_universe_master
  const preregistration = data.data_gates.preregistration
  const rule = data.protocol.candidate_rule

  return <>
    <PageHeader title="早期强势股识别 V5 · 事件静默量价" />

    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="永久研究项目" /></div>
      <div><span>设计区</span><strong>{data.design_years[0]}—{data.design_years.at(-1)}</strong></div>
      <div><span>冻结验证</span><StatusBadge status="SEALED" label={data.frozen_validation_years.join('/')} /></div>
      <div><span>前瞻观察</span><StatusBadge status="SEALED" label={String(data.observation_years[0])} /></div>
      <div><span>交易信号</span><strong>0</strong></div>
    </section>

    <section className="panel early-winner-safety">
      <div><LockKeyhole size={18} /><span><strong>历史母表未通过，V5 只完成预注册</strong><small>{master?.detail ?? '必须先补齐退市、转板和换码证券，再重建 2018—2023。当前不读取 2024/2025，不生成候选，不训练模型，也不创建交易部署。'}</small></span></div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>不可变研究协议</h2><span>任何规则变更必须新建 V6</span></div>
      <div className="early-winner-gates">
        <article><FileLock2 size={18} /><strong>候选规则</strong><p>公告事件分严格大于 0；硬负面优先阻断；再按成交额量比从低到高排序。</p></article>
        <article><FileLock2 size={18} /><strong>组合规则</strong><p>Top {rule.portfolio_size}、单行业最多 {rule.maximum_per_industry} 只；次日不可成交的名额留作现金，不递补。</p></article>
        <article><FileLock2 size={18} /><strong>验证规则</strong><p>{data.protocol.evaluation.non_overlap_phases} 个相位与 {data.protocol.evaluation.baseline} 配对，持有 {data.protocol.evaluation.holding_trading_days} 个交易日，并执行双倍成本压力测试。</p></article>
        <article><FileLock2 size={18} /><strong>失败规则</strong><p>冻结验证失败即否决；样本不足则标记不确定。V5 不在测试集调参，任何改变都建立 V6。</p></article>
      </div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>数据与封存门禁</h2><span>只读状态，不提供运行按钮</span></div>
      <div className="early-winner-gates">
        {Object.entries(data.data_gates).map(([name, gate]) => <article key={name}>
          <StatusBadge status={gate.ready ? 'READY' : gate.status ?? 'FAILED'} />
          <strong>{name}</strong><p>{gate.detail ?? (gate.ready ? '已冻结' : '尚未完成')}</p>
        </article>)}
      </div>
    </section>

    <section className="panel early-winner-safety">
      <div><DatabaseZap size={18} /><span><strong>协议哈希</strong><small className="symbol">{data.protocol_hash}</small></span></div>
      <div><FileLock2 size={18} /><span><strong>预注册</strong><small>{preregistration?.ready ? '协议已锁定；冻结年份仍未开放。' : '预注册证据未通过。'}</small></span></div>
    </section>
  </>
}
