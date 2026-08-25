import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { DatabaseZap, FlaskConical, LockKeyhole } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent } from '../components'

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function metricPercent(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) ? percent(number) : '—'
}

export default function EarlyWinnerV3Page() {
  const queryClient = useQueryClient()
  const project = useQuery({ queryKey: ['early-winner-v3'], queryFn: api.earlyWinnerV3, refetchInterval: 5_000 })
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['early-winner-v3'] })
    queryClient.invalidateQueries({ queryKey: ['jobs'] })
  }
  const supplement = useMutation({ mutationFn: api.supplementEarlyWinnerV3, onSuccess: invalidate })
  const audit = useMutation({ mutationFn: api.auditEarlyWinnerV3, onSuccess: invalidate })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />
  const data = project.data!
  const validation = data.latest_development_audit
  const gates = validation?.gates ?? {}
  const technical = record(validation?.ml_metrics)
  const technicalYears = record(technical.yearly)

  return <>
    <PageHeader title="早期强势股识别 V3" actions={<>
      <button className="button secondary" disabled={supplement.isPending || audit.isPending} onClick={() => supplement.mutate()}><DatabaseZap size={16} />重建点时补数</button>
      <button className="button" disabled={supplement.isPending || audit.isPending} onClick={() => audit.mutate()}><FlaskConical size={16} />运行开发审计</button>
    </>} />

    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="研究开发" /></div>
      <div><span>补数范围</span><strong>2018—2023</strong></div>
      <div><span>冻结验证</span><StatusBadge status="SEALED" label="2024/2025 封存" /></div>
      <div><span>前瞻集</span><StatusBadge status="SEALED" label="2026 封存" /></div>
      <div><span>交易信号</span><strong>0</strong></div>
    </section>

    <section className="panel early-winner-safety">
      <div><LockKeyhole size={18} /><span><strong>数据修复不等于策略通过</strong><small>V3 仅恢复历史换手率与点时 PE；开发期逐年门禁未全部通过时，不读取冻结测试期、不生成候选、不接交易。</small></span></div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>点时数据门禁</h2><span>字段来源、覆盖率和冻结状态</span></div>
      <div className="early-winner-gates">
        {Object.entries(data.data_gates).map(([name, gate]) => <article key={name}>
          <StatusBadge status={gate.ready ? 'READY' : gate.status ?? 'FAILED'} />
          <strong>{name}</strong><p>{gate.detail ?? '—'}</p>
        </article>)}
      </div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>开发期稳定性</h2><span>必须逐年通过 2020—2023</span></div>
      {Object.keys(gates).length ? <div className="table-wrap"><table><thead><tr><th>方案</th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>结论</th></tr></thead><tbody>
        {Object.entries(gates).map(([name, gate]) => <tr key={name}><td><strong>{name}</strong></td>
          {[2020, 2021, 2022, 2023].map((year) => <td key={year}><StatusBadge status={gate.yearly?.[String(year)] ? 'PASSED' : 'FAILED'} /></td>)}
          <td><StatusBadge status={gate.passed ? 'PASSED' : 'REJECTED'} /></td></tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>技术方案逐年结果</h2><span>非重叠 60 日资金周期</span></div>
      {Object.keys(technicalYears).length ? <div className="table-wrap"><table><thead><tr><th>年份</th><th>Precision@20</th><th>收益</th><th>双倍成本</th><th>最大回撤</th><th>门禁</th></tr></thead><tbody>
        {Object.entries(technicalYears).map(([year, value]) => { const metrics = record(value); return <tr key={year}><td><strong>{year}</strong></td><td>{metricPercent(metrics.precision_at_20)}</td><td>{metricPercent(metrics.total_return)}</td><td>{metricPercent(metrics.double_cost_return)}</td><td>{metricPercent(metrics.max_drawdown)}</td><td><StatusBadge status={metrics.gate_passed ? 'PASSED' : 'FAILED'} /></td></tr> })}
      </tbody></table></div> : <EmptyState />}
    </section>
  </>
}
