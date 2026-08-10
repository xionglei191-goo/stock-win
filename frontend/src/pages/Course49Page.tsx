import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, time } from '../components'
import type { PlaybookState } from '../types'

const phaseNames: Record<string, string> = {
  RECOVERY: '修复', FERMENT: '发酵', ACCELERATION: '加速',
  DIVERGENCE: '分歧', CLIMAX: '高潮', RETREAT: '退潮', ICE: '冰点',
  NORMAL: '常态',
}

const styleNames: Record<string, string> = {
  SMALL_CAP_SPECULATION: '小盘投机', BROAD_RISK_ON: '全面风险偏好',
  MIXED: '混合风格', GROWTH_TREND: '成长趋势',
  LARGE_CAP_TREND: '大盘趋势', DEFENSIVE: '防御', UNKNOWN: '数据不足',
}

const playbookNames: Record<string, string> = {
  recovery_ignition: '修复启动',
  ferment_second_board: '发酵二板',
  acceleration_core_relay: '加速核心接力',
}

const reasonNames: Record<string, string> = {
  market_ecology_not_entry_ready: '市场生态不适合开仓',
  missing_critical_benchmark: '关键风格基准缺失',
  ENTRY_NOT_ALLOWED: '当前不允许新开仓',
  PLAYBOOK_NOT_PRODUCTION: '剧本尚未进入生产状态',
  MARKET_PHASE_RECOVERY: '当前不是修复剧本阶段',
  MARKET_PHASE_FERMENT: '当前不是发酵剧本阶段',
  MARKET_PHASE_ACCELERATION: '当前不是加速剧本阶段',
  MARKET_PHASE_DIVERGENCE: '市场处于分歧阶段',
  MARKET_PHASE_CLIMAX: '市场处于高潮阶段',
  MARKET_PHASE_RETREAT: '市场处于退潮阶段',
  MARKET_PHASE_ICE: '市场处于冰点阶段',
  MARKET_PHASE_NORMAL: '市场阶段尚未触发剧本',
}

function reasonName(value: string) {
  return reasonNames[value] ?? value
}

const funnelNames: Array<[string, string]> = [
  ['market', '全市场'], ['eligible', '合格股票'], ['strong_themes', '强题材'],
  ['leaders', '龙头'], ['playbook_hits', '剧本命中'], ['routed', '最终路由'],
]

function objectValue(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {}
}

function arrayValue(value: unknown): Array<Record<string, unknown>> {
  return Array.isArray(value)
    ? value.filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
    : []
}

function numberValue(value: unknown) {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function percent(value: unknown) {
  return new Intl.NumberFormat('zh-CN', {
    style: 'percent', maximumFractionDigits: 1,
  }).format(numberValue(value))
}

export default function Course49Page() {
  const client = useQueryClient()
  const [jobId, setJobId] = useState<string | null>(null)
  const detail = useQuery({
    queryKey: ['framework', 'course49'],
    queryFn: () => api.framework('course49'),
    refetchInterval: 30_000,
  })
  const scan = useMutation({
    mutationFn: () => api.scanSelection(['course49_system']),
    onSuccess: (result) => setJobId(result.job_id),
  })
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.job(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => ['QUEUED', 'RUNNING'].includes(query.state.data?.status ?? '') ? 1_500 : false,
  })

  useEffect(() => {
    if (job.data?.status === 'SUCCEEDED') {
      client.invalidateQueries({ queryKey: ['framework', 'course49'] })
      client.invalidateQueries({ queryKey: ['signals'] })
    }
  }, [client, job.data?.status])

  const state = detail.data?.state ?? {}
  const funnel = objectValue(state.funnel)
  const completeness = Object.values(objectValue(state.data_completeness))
  const playbookStates = useMemo(
    () => arrayValue(state.playbook_states) as unknown as PlaybookState[],
    [state.playbook_states],
  )
  const playbookStateMap = new Map(playbookStates.map((item) => [item.playbook_id, item]))
  const runtimeMap = new Map(
    (detail.data?.runtime_states ?? []).map((item) => [item.scope, item.state]),
  )
  const activeJob = scan.isPending || ['QUEUED', 'RUNNING'].includes(job.data?.status ?? '')

  if (detail.isLoading) return <LoadingState />
  if (detail.error) return <ErrorState error={detail.error} />
  if (!detail.data) return <EmptyState />

  return <>
    <PageHeader
      title="49课工作台"
      actions={<button className="button" type="button" disabled={activeJob} onClick={() => scan.mutate()}>
        <RefreshCw size={16} className={activeJob ? 'spin' : ''} />
        {activeJob ? '扫描中' : '运行体系扫描'}
      </button>}
    />

    <section className="framework-context panel">
      <div className="section-heading">
        <div><h2>{detail.data.name}</h2><span>策略版本 {detail.data.version} · 规则版本 {detail.data.policy_version}</span></div>
        <StatusBadge status={state.entry_allowed ? 'READY' : 'WAIT'} label={state.entry_allowed ? '允许新开仓' : '仅管理持仓'} />
      </div>
      <div className="context-facts">
        <div><span>市场阶段</span><strong>{phaseNames[String(state.market_phase)] ?? String(state.market_phase || '-')}</strong></div>
        <div><span>市场风格</span><strong>{styleNames[String(state.market_style)] ?? String(state.market_style || '-')}</strong></div>
        <div><span>适用度</span><strong>{percent(state.style_suitability)}</strong></div>
        <div><span>数据时点</span><strong>{String(state.asof ?? '-')}</strong></div>
        <div><span>上下文版本</span><strong>{String(state.context_version ?? '-')}</strong></div>
        <div><span>数据完整性</span><strong>{completeness.length && completeness.every(Boolean) ? '完整' : '有限'}</strong></div>
      </div>
      {state.entry_block_reason ? <div className="framework-block-reason">当前阻断：{reasonName(String(state.entry_block_reason))}</div> : null}
    </section>

    <section className="framework-section">
      <div className="section-heading"><h2>候选漏斗</h2><span>共享上下文只构建一次</span></div>
      <div className="funnel-strip">
        {funnelNames.map(([key, label], index) => <div key={key} className="funnel-step">
          <span>{label}</span><strong>{numberValue(funnel[key])}</strong>
          {index < funnelNames.length - 1 && <i aria-hidden="true">›</i>}
        </div>)}
      </div>
    </section>

    <section className="framework-section">
      <div className="section-heading"><h2>生产剧本</h2><span>仅生产状态可进入扫描与模拟账户</span></div>
      <div className="playbook-grid">
        {detail.data.playbooks.map((playbook) => {
          const current = playbookStateMap.get(playbook.playbook_id)
          const blocked = current?.blocked_reasons ?? []
          return <article className="playbook-card" key={playbook.playbook_id}>
            <div><strong>{playbook.name}</strong><StatusBadge status={current?.admitted ? 'READY' : 'WAIT'} label={current?.admitted ? '已准入' : '未准入'} /></div>
            <p>{playbook.description}</p>
            <dl>
              <div><dt>适用阶段</dt><dd>{phaseNames[playbook.market_phase] ?? playbook.market_phase}</dd></div>
              <div><dt>基础权重</dt><dd>{percent(playbook.base_weight)}</dd></div>
              <div><dt>当日预算</dt><dd>{percent(current?.budget)}</dd></div>
              <div><dt>候选 / 路由</dt><dd>{current?.candidate_count ?? 0} / {current?.routed_count ?? 0}</dd></div>
            </dl>
            <small>{blocked.length ? blocked.map(reasonName).join(' / ') : '规则条件满足后进入统一路由'}</small>
          </article>
        })}
      </div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>路由候选</h2><span>{detail.data.candidates.length} 条评估结果</span></div>
      {detail.data.candidates.length ? <div className="table-wrap"><table className="framework-candidates">
        <thead><tr><th>排名</th><th>代码</th><th>剧本</th><th>题材</th><th>角色</th><th className="numeric">连板</th><th className="numeric">封板质量</th><th className="numeric">路由分</th><th className="numeric">目标权重</th><th>状态</th></tr></thead>
        <tbody>{detail.data.candidates.map((item, index) => <tr key={String(item.code) + String(item.playbook_id) + index}>
          <td>{numberValue(item.route_rank) || '-'}</td>
          <td className="symbol">{String(item.code ?? '-')}</td>
          <td>{playbookNames[String(item.playbook_id)] ?? String(item.playbook_id ?? '-')}</td>
          <td>{String(item.sector_name ?? item.sector_code ?? '-')}</td>
          <td>{String(item.role ?? '-')}</td>
          <td className="numeric">{numberValue(item.streak)}</td>
          <td className="numeric">{percent(item.board_quality_score)}</td>
          <td className="numeric">{percent(item.route_score)}</td>
          <td className="numeric">{percent(item.target_weight)}</td>
          <td><StatusBadge status={String(item.status ?? 'WAIT')} /></td>
        </tr>)}</tbody>
      </table></div> : <EmptyState>当前没有命中生产剧本的候选</EmptyState>}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>持仓风险</h2><span>{detail.data.positions.length} 个持仓</span></div>
      {detail.data.positions.length ? <div className="table-wrap"><table>
        <thead><tr><th>代码</th><th>来源剧本</th><th>入场日</th><th className="numeric">成本</th><th className="numeric">最新价</th><th className="numeric">市场弱势</th><th className="numeric">题材弱势</th><th className="numeric">龙头失位</th><th className="numeric">最高收盘</th></tr></thead>
        <tbody>{detail.data.positions.map((position) => {
          const evidence = objectValue(position.evidence)
          const runtime = runtimeMap.get(position.code) ?? {}
          return <tr key={position.code}>
            <td className="symbol">{position.code}</td>
            <td>{playbookNames[String(evidence.playbook_id)] ?? String(evidence.playbook_id ?? '-')}</td>
            <td>{position.entry_time}</td>
            <td className="numeric">{position.average_price.toFixed(2)}</td>
            <td className="numeric">{position.last_price.toFixed(2)}</td>
            <td className="numeric">{numberValue(runtime.market_weak_days)} 日</td>
            <td className="numeric">{numberValue(runtime.sector_weak_days)} 日</td>
            <td className="numeric">{numberValue(runtime.leader_weak_days)} 日</td>
            <td className="numeric">{numberValue(runtime.max_close).toFixed(2)}</td>
          </tr>
        })}</tbody>
      </table></div> : <EmptyState>体系账户当前没有持仓</EmptyState>}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>历史状态</h2><span>{detail.data.latest_backtest ? '最近回测 ' + String(detail.data.latest_backtest.start_date ?? '') + ' 至 ' + String(detail.data.latest_backtest.end_date ?? '') : '尚无体系回测'}</span></div>
      {detail.data.history.length ? <div className="table-wrap"><table>
        <thead><tr><th>日期</th><th>市场阶段</th><th>市场风格</th><th className="numeric">适用度</th><th>交易模式</th><th>新开仓</th></tr></thead>
        <tbody>{detail.data.history.slice(0, 30).map((item) => <tr key={item.timestamp}>
          <td>{item.timestamp}</td><td>{phaseNames[item.market_phase] ?? item.market_phase}</td>
          <td>{styleNames[item.market_style] ?? item.market_style}</td>
          <td className="numeric">{percent(item.suitability)}</td><td>{item.trade_mode || '观察'}</td>
          <td><StatusBadge status={item.entry_allowed ? 'READY' : 'WAIT'} label={item.entry_allowed ? '允许' : '阻断'} /></td>
        </tr>)}</tbody>
      </table></div> : <EmptyState>完成一次体系回测后显示历史时间线</EmptyState>}
    </section>

    {job.data && ['QUEUED', 'RUNNING'].includes(job.data.status) && <div className="toast">
      {job.data.detail || job.data.phase} · {Math.round((job.data.progress ?? 0) * 100)}%
    </div>}
    {(scan.error || job.data?.status === 'FAILED') && <div className="toast toast--error">{scan.error?.message || job.data?.error}</div>}
    {job.data?.status === 'SUCCEEDED' && <div className="toast">体系扫描已完成，数据更新于 {time(job.data.finished_at ?? undefined)}</div>}
  </>
}
