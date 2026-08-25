import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, LockKeyhole } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent } from '../components'

function metricPercent(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) ? percent(number) : '—'
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

export default function EarlyWinnerV2Page() {
  const queryClient = useQueryClient()
  const project = useQuery({
    queryKey: ['early-winner-v2'],
    queryFn: api.earlyWinnerV2,
    refetchInterval: 5_000,
  })
  const audit = useMutation({
    mutationFn: api.auditEarlyWinnerV2,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['early-winner-v2'] })
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />
  const data = project.data!
  const validation = data.latest_development_audit
  const gates = validation?.gates ?? {}
  const technical = record(validation?.ml_metrics)
  const technicalYears = record(technical.yearly)

  return <>
    <PageHeader
      title="早期强势股识别 V2"
      actions={<><a className="button secondary" href="/research/early-winner-v3">查看 V3 点时补数</a><button className="button" disabled={audit.isPending} onClick={() => audit.mutate()}><FlaskConical size={16} />运行开发期审计</button></>}
    />

    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="研究开发" /></div>
      <div><span>开发区</span><strong>2018—2023</strong></div>
      <div><span>禁止调参</span><strong>{data.excluded_tuning_years.join('、')}</strong></div>
      <div><span>前瞻集</span><StatusBadge status="SEALED" label="2026 封存" /></div>
      <div><span>交易信号</span><strong>0</strong></div>
    </section>

    <section className="panel early-winner-safety">
      <div><LockKeyhole size={18} /><span><strong>V1 与前瞻集隔离</strong><small>V2 不读取 2024/2025 做调参；开发期全部年份通过前，不训练冻结模型、不生成候选、不打开 2026。</small></span></div>
      {audit.data && <small>任务 {audit.data.job_id.slice(0, 8)} · {audit.data.status}</small>}
      {audit.error && <small className="negative-text">{audit.error.message}</small>}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>开发数据门禁</h2><span>任一失败都保持研究封存</span></div>
      <div className="early-winner-gates">
        {Object.entries(data.data_gates).map(([name, gate]) => <article key={name}>
          <StatusBadge status={gate.ready ? 'READY' : gate.status ?? 'FAILED'} />
          <strong>{name}</strong><p>{gate.detail ?? '—'}</p>
          {gate.row_count != null && <small>{gate.row_count} 条</small>}
        </article>)}
      </div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>方案稳定性</h2><span>必须逐年通过 2020—2023</span></div>
      {Object.keys(gates).length ? <div className="table-wrap"><table><thead><tr><th>方案</th><th>2020</th><th>2021</th><th>2022</th><th>2023</th><th>结论</th></tr></thead><tbody>
        {Object.entries(gates).map(([name, gate]) => <tr key={name}>
          <td><strong>{name}</strong></td>
          {[2020, 2021, 2022, 2023].map((year) => <td key={year}><StatusBadge status={gate.yearly?.[String(year)] ? 'PASSED' : 'FAILED'} /></td>)}
          <td><StatusBadge status={gate.passed ? 'PASSED' : 'REJECTED'} /></td>
        </tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>技术/行业方案逐年结果</h2><span>收益采用非重叠 60 日资金周期</span></div>
      {Object.keys(technicalYears).length ? <div className="table-wrap"><table><thead><tr><th>年份</th><th>Precision@20</th><th>PR-AUC</th><th>收益</th><th>双倍成本</th><th>最大回撤</th><th>门禁</th></tr></thead><tbody>
        {Object.entries(technicalYears).map(([year, value]) => {
          const metrics = record(value)
          return <tr key={year}><td><strong>{year}</strong></td><td>{metricPercent(metrics.precision_at_20)}</td><td>{Number(metrics.pr_auc ?? 0).toFixed(3)}</td><td>{metricPercent(metrics.total_return)}</td><td>{metricPercent(metrics.double_cost_return)}</td><td>{metricPercent(metrics.max_drawdown)}</td><td><StatusBadge status={metrics.gate_passed ? 'PASSED' : 'FAILED'} /></td></tr>
        })}
      </tbody></table></div> : <EmptyState />}
    </section>
  </>
}
