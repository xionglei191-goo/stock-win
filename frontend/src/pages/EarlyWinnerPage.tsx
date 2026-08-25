import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BrainCircuit, Database, FlaskConical, RefreshCw } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent } from '../components'
import type { EarlyWinnerCandidate } from '../types'

function metric(value: unknown, digits = 2) {
  const number = Number(value)
  return Number.isFinite(number) ? number.toFixed(digits) : '—'
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {}
}

function metricPercent(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) ? percent(number) : '—'
}

function factor(row: EarlyWinnerCandidate, primary: string, fallback: string) {
  return row.factor[primary] ?? row.factor[fallback]
}

function candidateTable(title: string, rows: EarlyWinnerCandidate[]) {
  return <section className="table-section early-winner-candidates">
    <div className="section-heading"><h2>{title}</h2><span>{rows.length} 只</span></div>
    {rows.length ? <div className="table-wrap"><table><thead><tr><th>排名</th><th>股票</th><th>行业</th><th>分数 / 概率</th><th>行业</th><th>基本面</th><th>动量</th><th>突破</th><th>事件</th><th>资金</th><th>过热扣分</th><th>证据</th></tr></thead><tbody>
      {rows.map((row) => <tr key={row.candidate_id}>
        <td>{row.rank}</td>
        <td><strong>{row.name || row.code}</strong><small className="subline">{row.code}</small></td>
        <td>{row.industry || '未分类'}</td>
        <td><strong>{metric(row.score)}</strong>{row.probability != null && <small className="subline">{percent(row.probability)}</small>}</td>
        <td>{metric(factor(row, 'industry', 'industry_momentum'))}</td>
        <td>{metric(factor(row, 'fundamental', 'revenue_yoy'))}</td>
        <td>{metric(factor(row, 'momentum', 'relative_return_60'))}</td>
        <td>{metric(factor(row, 'breakout', 'breakout_distance'))}</td>
        <td>{metric(factor(row, 'event', 'event_score'))}</td>
        <td>{metric(factor(row, 'flow', 'northbound_change_ratio'))}</td>
        <td>{metric(factor(row, 'heat_penalty', 'price_to_ma60'))}</td>
        <td>{row.evidence_refs.length ? <details><summary>{row.evidence_refs.length} 项</summary><div className="evidence-list">{row.evidence_refs.map((item) => <code key={item}>{item}</code>)}</div></details> : '—'}</td>
      </tr>)}
    </tbody></table></div> : <EmptyState />}
  </section>
}

export default function EarlyWinnerPage() {
  const queryClient = useQueryClient()
  const project = useQuery({
    queryKey: ['early-winner'],
    queryFn: api.earlyWinner,
    refetchInterval: 5_000,
  })
  const run = useMutation({
    mutationFn: (action: 'refresh' | 'train' | 'validate') => {
      if (action === 'refresh') return api.refreshEarlyWinner()
      if (action === 'train') return api.trainEarlyWinner()
      return api.validateEarlyWinner()
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['early-winner'] })
      queryClient.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />
  const data = project.data!
  const historyTrust = data.history?.trust_policy
  const legacyQuarantined = historyTrust?.ready === false
  const validation = data.latest_validation ?? {}
  const validationGates = (validation.gates ?? {}) as Record<string, { passed?: boolean }>
  const model = data.latest_model ?? {}
  const methodMetrics = [
    ['规则', record(validation.rule_metrics)],
    ['ML', record(validation.ml_metrics)],
    ['纯 RS60', record(validation.baseline_metrics)],
  ] as const

  return <>
    <PageHeader
      title="早期强势股识别"
      actions={<>
        <a className="button button--secondary" href="/research/early-winner-v2">查看 V2</a>
        <button className="button button--secondary" disabled={run.isPending || legacyQuarantined} onClick={() => run.mutate('refresh')}><RefreshCw size={16} />更新数据</button>
        <button className="button button--secondary" disabled={run.isPending || legacyQuarantined} onClick={() => run.mutate('train')}><BrainCircuit size={16} />训练 ML</button>
        <button className="button" disabled={run.isPending || legacyQuarantined} onClick={() => run.mutate('validate')}><FlaskConical size={16} />运行验证</button>
      </>}
    />

    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="研究观察" /></div>
      <div><span>数据时点</span><strong>{data.data_asof ?? '—'}</strong></div>
      <div><span>规则候选</span><strong>{data.candidates.rule.length}</strong></div>
      <div><span>ML 候选</span><strong>{data.candidates.ml.length}</strong></div>
      <div><span>两榜重合</span><strong>{data.overlap.length}</strong></div>
      <div><span>交易信号</span><strong>0</strong></div>
    </section>

    <section className="panel early-winner-safety">
      <div><Database size={18} /><span><strong>研究隔离已启用</strong><small>无审批、交易、模拟账户和通达信推送入口；转正式策略必须另行立项。</small></span></div>
      {run.data && <small>任务 {run.data.job_id.slice(0, 8)} · {run.data.status}</small>}
      {run.error && <small className="negative-text">{run.error.message}</small>}
    </section>

    {legacyQuarantined && <section className="panel early-winner-safety">
      <div><Database size={18} /><span><strong>V1 历史证据已撤销资格</strong><small>旧文件仍保留审计，但历史股票池存在幸存者偏差，不能再用于更新、训练或验证。后续只在新的密封版本中继续。</small></span></div>
      <StatusBadge status={String(historyTrust?.status ?? 'BLOCKED_DATA')} />
    </section>}

    <section className="table-section">
      <div className="section-heading"><h2>数据门禁</h2><span>任一必需门禁失败即停止运行</span></div>
      <div className="early-winner-gates">
        {Object.entries(data.data_gates).map(([name, gate]) => <article key={name}>
          <StatusBadge status={gate.ready === false ? 'FAILED' : gate.status ?? 'UNKNOWN'} />
          <strong>{name}</strong>
          <p>{gate.detail ?? '—'}</p>
          {gate.row_count != null && <small>{gate.row_count} 条</small>}
        </article>)}
      </div>
    </section>

    <section className="early-winner-summary-grid">
      <article className="panel">
        <div className="section-heading"><h2>模型版本</h2><StatusBadge status={String(model.status ?? 'NOT_TRAINED')} /></div>
        <dl className="early-winner-facts">
          <div><dt>模型 ID</dt><dd>{String(model.model_id ?? '—')}</dd></div>
          <div><dt>训练窗口</dt><dd>{String(model.training_start ?? '—')} — {String(model.training_end ?? '—')}</dd></div>
          <div><dt>随机种子</dt><dd>{String(model.random_seed ?? 49)}</dd></div>
          <div><dt>sklearn</dt><dd>{String(model.library_version ?? '—')}</dd></div>
        </dl>
      </article>
      <article className="panel">
        <div className="section-heading"><h2>验证结论</h2><StatusBadge status={String(validation.status ?? 'NOT_VALIDATED')} /></div>
        <div className="gate-list">
          {Object.entries(validationGates).map(([name, gate]) => <span key={name} className={gate.passed ? 'passed' : 'failed'}>{name} · {gate.passed ? '通过' : '未通过'}</span>)}
        </div>
        {!Object.keys(validationGates).length && <EmptyState />}
      </article>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>同快照验证对照</h2><span>基础成本、双倍成本与头部样本归因</span></div>
      {data.latest_validation ? <div className="table-wrap"><table><thead><tr><th>方法</th><th>Precision@20</th><th>PR-AUC</th><th>IC</th><th>组合收益</th><th>双倍成本</th><th>Sharpe</th><th>Calmar</th><th>最大回撤</th><th>换手率</th><th>剔除最高5只</th></tr></thead><tbody>
        {methodMetrics.map(([name, values]) => <tr key={name}>
          <td><strong>{name}</strong></td>
          <td>{metricPercent(values.precision_at_20)}</td>
          <td>{metric(values.pr_auc, 3)}</td>
          <td>{metric(values.ic, 3)}</td>
          <td>{metricPercent(values.total_return)}</td>
          <td>{metricPercent(values.double_cost_return)}</td>
          <td>{metric(values.sharpe)}</td>
          <td>{metric(values.calmar)}</td>
          <td>{metricPercent(values.max_drawdown)}</td>
          <td>{metricPercent(values.turnover)}</td>
          <td>{metricPercent(values.without_top_five_total_return)}</td>
        </tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>数据批次审计</h2><span>原始文件与模型文件保存在 Git 外，页面展示索引和哈希</span></div>
      {data.latest_batches.length ? <div className="table-wrap"><table><thead><tr><th>数据集</th><th>来源</th><th>状态</th><th>行数</th><th>截至</th><th>内容哈希</th></tr></thead><tbody>
        {data.latest_batches.map((batch, index) => <tr key={String(batch.batch_id ?? index)}>
          <td><strong>{String(batch.dataset ?? '—')}</strong><small className="subline">{String(batch.batch_id ?? '')}</small></td>
          <td>{String(batch.source ?? '—')}</td>
          <td><StatusBadge status={String(batch.status ?? 'UNKNOWN')} /></td>
          <td>{String(batch.row_count ?? '—')}</td>
          <td>{String(batch.published_end ?? batch.fetched_at ?? '—')}</td>
          <td><code>{String(batch.content_hash ?? '—').slice(0, 16)}</code></td>
        </tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    {data.overlap.length > 0 && <section className="panel"><div className="section-heading"><h2>两榜重合</h2><span>{data.overlap.length} 只</span></div><div className="gate-list">{data.overlap.map((code) => <span className="passed" key={code}>{code}</span>)}</div></section>}

    <div className="early-winner-lists">
      {candidateTable('规则 Top20', data.candidates.rule)}
      {candidateTable('ML Top20', data.candidates.ml)}
    </div>
  </>
}
