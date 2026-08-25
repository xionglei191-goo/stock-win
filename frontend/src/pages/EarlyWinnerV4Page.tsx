import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DatabaseZap, FlaskConical, LockKeyhole } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent } from '../components'
import type { EarlyWinnerV4DataGate, EarlyWinnerV4Project } from '../types'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function metricPercent(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) ? percent(number) : '—'
}

const DATASET_LABELS: Record<string, string> = {
  raw_execution_bars: '未复权执行行情',
  adjusted_bars_factors: '前复权行情与因子',
  trading_calendar: '官方交易日历',
  financial_reports: '结构化财务报告',
  earnings_guidance_express: '业绩预告与快报',
  gp15_price_limits: '逐日涨跌停价格',
  gp29_st_status: 'ST/退市整理状态',
  gp30_corporate_actions: '公司行动 GP30',
  gp43_corporate_actions: '公司行动 GP43',
  industry_history: '历史行业分类',
  announcement_documents: '公告原文',
  suspension_status: '独立停复牌状态',
}

function datasetLabel(value: string) {
  return DATASET_LABELS[value] ?? value
}

function safetyCopy(status: EarlyWinnerV4Project['status'], delistedGate?: EarlyWinnerV4DataGate) {
  if (status === 'DEVELOPMENT_REJECTED') return {
    title: 'V4 已被开发期否决，不能进入交易',
    detail: '目标为未来40日正收益且同期前10%；2020—2023 未逐年通过相对 RS60、双倍成本、绝对正收益和回撤门禁，因此不读取冻结测试期、不生成候选、不创建交易部署。',
  }
  if (status === 'DEVELOPMENT_READY') return {
    title: 'V4 开发期门禁已通过，冻结验证仍未开放',
    detail: '当前只能查看开发审计证据；2024/2025 冻结验证必须另行实施并通过，本页仍不生成候选或交易信号。',
  }
  if (status === 'DEVELOPMENT_AUDITING') return {
    title: 'V4 正在运行开发期审计',
    detail: '审计完成前 2024/2025 保持封存，候选生成和交易链路保持关闭。',
  }
  if (status === 'DEVELOPMENT_AUDIT_REQUIRED') return {
    title: 'V4 40日标签已就绪，等待开发期审计',
    detail: '只允许运行开发审计；开发门禁完成前不读取冻结测试期，不生成候选或交易信号。',
  }
  if (status === 'BLOCKED_DATA') return {
    title: 'V4 已被数据门禁阻断',
    detail: delistedGate && !delistedGate.ready
      ? `历史证券母表已修复，但退市证券逐时点数据仅验证 ${delistedGate.source_dataset_count ?? 0}/${delistedGate.required_source_dataset_count ?? 12} 类。其余来源补齐并通过全量冷重放前，不重建标签、不读取冻结测试期、不生成候选或交易信号。`
      : '上游历史证据尚未全部通过；阻断期间不重建标签、不读取冻结测试期、不生成候选或交易信号。',
  }
  return {
    title: 'V4 正在构建40日标签快照',
    detail: '数据与时点审计完成前开发审计、冻结验证、候选生成和交易链路全部关闭。',
  }
}

export default function EarlyWinnerV4Page() {
  const queryClient = useQueryClient()
  const project = useQuery({ queryKey: ['early-winner-v4'], queryFn: api.earlyWinnerV4, refetchInterval: 5_000 })
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['early-winner-v4'] })
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }
  const build = useMutation({ mutationFn: api.buildLabelsEarlyWinnerV4, onSuccess: invalidate })
  const audit = useMutation({ mutationFn: api.auditEarlyWinnerV4, onSuccess: invalidate })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />
  const data = project.data!
  const validation = data.latest_development_audit
  const model = record(validation?.ml_metrics)
  const yearly = record(model.yearly)
  const baseline = record(validation?.baseline_metrics)
  const baselineYearly = record(baseline.yearly)
  const gates = validation?.gates ?? {}
  const frozenGate = data.data_gates.frozen_2024_2025
  const universeGate = data.data_gates.historical_universe_master
  const delistedGate = data.data_gates.delisted_history_quality
  const safety = safetyCopy(data.status, delistedGate)
  const validationRequired = data.status === 'DEVELOPMENT_READY' || frozenGate?.status === 'VALIDATION_REQUIRED'
  const upstreamDataReady = universeGate?.ready === true && delistedGate?.ready === true
  const missingDatasets = delistedGate?.missing_source_datasets ?? []
  const verifiedDatasets = delistedGate?.source_datasets ?? []
  const findingCounts = Object.entries(delistedGate?.finding_counts ?? {})
    .filter(([, count]) => Number(count) > 0)
    .sort((left, right) => Number(right[1]) - Number(left[1]))

  return <>
    <PageHeader title="早期强势股识别 V4" actions={<>
      <button className="button button--secondary" disabled={!upstreamDataReady || build.isPending || audit.isPending} onClick={() => build.mutate()}><DatabaseZap size={16} />重建40日标签</button>
      <button className="button" disabled={!upstreamDataReady || build.isPending || audit.isPending} onClick={() => audit.mutate()}><FlaskConical size={16} />运行开发审计</button>
    </>} />
    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="研究开发" /></div>
      <div><span>持有期</span><strong>{data.protocol.holding_trading_days} 个交易日</strong></div>
      <div><span>市场门禁</span><strong>MA60 广度 &gt; {percent(data.protocol.market_breadth_threshold)}</strong></div>
      <div><span>冻结验证</span><StatusBadge status={validationRequired ? 'VALIDATION_REQUIRED' : 'SEALED'} label={validationRequired ? '2024/2025 待验证' : '2024/2025 封存'} /></div>
      <div><span>交易信号</span><strong>0</strong></div>
    </section>
    <section className="panel early-winner-safety">
      <div><LockKeyhole size={18} /><span><strong>{safety.title}</strong><small>{safety.detail}</small></span></div>
    </section>
    {universeGate && !universeGate.ready ? <section className="panel early-winner-safety">
      <div><DatabaseZap size={18} /><span><strong>历史证券母表未通过</strong><small>{universeGate.detail ?? '当前证券列表无法证明覆盖历史退市、转板和换码证券。'}</small></span></div>
    </section> : null}
    {delistedGate && !delistedGate.ready ? <section className="table-section">
      <div className="section-heading"><h2>退市历史证据门禁</h2><span>逐原始对象冷重放；部分来源不能晋级</span></div>
      <section className="metrics-band metrics-band--compact">
        <div><span>已验证</span><strong>{delistedGate.source_dataset_count ?? verifiedDatasets.length}/{delistedGate.required_source_dataset_count ?? 12} 类</strong></div>
        <div><span>当前状态</span><StatusBadge status={delistedGate.status ?? 'BLOCKED_DATA'} /></div>
        <div><span>缺失来源</span><strong>{missingDatasets.length}</strong></div>
        <div><span>标签与审计</span><strong>关闭</strong></div>
      </section>
      {verifiedDatasets.length ? <p><strong>已验证：</strong>{verifiedDatasets.map(datasetLabel).join('、')}</p> : null}
      {missingDatasets.length ? <p><strong>仍缺：</strong>{missingDatasets.map(datasetLabel).join('、')}</p> : null}
      {findingCounts.length ? <div className="table-wrap"><table><thead><tr><th>失败证据</th><th>数量</th></tr></thead><tbody>
        {findingCounts.map(([name, count]) => <tr key={name}><td>{name}</td><td>{Number(count).toLocaleString()}</td></tr>)}
      </tbody></table></div> : null}
    </section> : null}
    <section className="table-section">
      <div className="section-heading"><h2>数据与协议门禁</h2><span>40日未复权执行标签与冻结边界</span></div>
      <div className="early-winner-gates">{Object.entries(data.data_gates).map(([name, gate]) => <article key={name}>
        <StatusBadge status={gate.ready ? 'READY' : gate.status ?? 'FAILED'} /><strong>{name}</strong><p>{gate.detail ?? '—'}</p>
      </article>)}</div>
    </section>
    <section className="table-section">
      <div className="section-heading"><h2>开发期样本外结果</h2><span>候选模型与纯 RS60 使用同一市场状态和执行口径</span></div>
      {Object.keys(yearly).length ? <div className="table-wrap"><table><thead><tr><th>年份</th><th>方法</th><th>Precision@20</th><th>收益</th><th>双倍成本</th><th>最大回撤</th><th>门禁</th></tr></thead><tbody>
        {Object.entries(yearly).flatMap(([year, value]) => { const metrics = record(value); const base = record(baselineYearly[year]); return [
          <tr key={`${year}-ml`}><td><strong>{year}</strong></td><td>V4 ML</td><td>{metricPercent(metrics.precision_at_20)}</td><td>{metricPercent(metrics.total_return)}</td><td>{metricPercent(metrics.double_cost_return)}</td><td>{metricPercent(metrics.max_drawdown)}</td><td><StatusBadge status={metrics.gate_passed ? 'PASSED' : 'FAILED'} /></td></tr>,
          <tr key={`${year}-rs`}><td>{year}</td><td>RS60 基准</td><td>{metricPercent(base.precision_at_20)}</td><td>{metricPercent(base.total_return)}</td><td>{metricPercent(base.double_cost_return)}</td><td>{metricPercent(base.max_drawdown)}</td><td>基准</td></tr>,
        ] })}
      </tbody></table></div> : <EmptyState />}
    </section>
    <section className="table-section">
      <div className="section-heading"><h2>逐年晋级门禁</h2><span>四年必须全部通过</span></div>
      {Object.keys(gates).length ? <div className="table-wrap"><table><thead><tr><th>方案</th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>结论</th></tr></thead><tbody>
        {Object.entries(gates).map(([name, gate]) => <tr key={name}><td><strong>{name}</strong></td>{[2020, 2021, 2022, 2023].map((year) => <td key={year}><StatusBadge status={gate.yearly?.[String(year)] ? 'PASSED' : 'FAILED'} /></td>)}<td><StatusBadge status={gate.passed ? 'PASSED' : 'REJECTED'} /></td></tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>
  </>
}
