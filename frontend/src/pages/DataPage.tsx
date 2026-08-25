import { useQuery } from '@tanstack/react-query'
import { useState } from 'react'
import { api } from '../api'
import { EmptyState, ErrorState, HealthIcon, LoadingState, PageHeader, StatusBadge, time } from '../components'
import type { USPaperGate } from '../types'

function compactId(value?: string | null) {
  return value ? value.slice(0, 12) : '-'
}

function date(value?: string | null) {
  return value ? value.slice(0, 10) : '-'
}

function usd(value?: number) {
  if (value == null || !Number.isFinite(value)) return '-'
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency', currency: 'USD', maximumFractionDigits: 2,
  }).format(value)
}

export function usPaperGateStatus(gate: string | USPaperGate | undefined) {
  if (typeof gate === 'string') return gate
  return gate?.status ?? 'UNAVAILABLE'
}

export default function DataPage() {
  const [selectedReleaseId, setSelectedReleaseId] = useState('')
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const sources = useQuery({ queryKey: ['sources'], queryFn: api.sources })
  const releases = useQuery({ queryKey: ['us-pit-releases'], queryFn: api.usPitReleases, refetchInterval: 60_000 })
  const paper = useQuery({ queryKey: ['us-paper-status'], queryFn: api.usPaperStatus, refetchInterval: 30_000 })
  const activeReleaseId = selectedReleaseId
    || releases.data?.find((release) => release.status === 'DATA_READY')?.release_id
    || releases.data?.[0]?.release_id
    || ''
  const release = useQuery({
    queryKey: ['us-pit-release', activeReleaseId],
    queryFn: () => api.usPitRelease(activeReleaseId),
    enabled: Boolean(activeReleaseId),
  })
  const quality = useQuery({
    queryKey: ['us-pit-quality', activeReleaseId],
    queryFn: () => api.usPitQuality(activeReleaseId),
    enabled: Boolean(activeReleaseId),
  })
  const selectedSummary = releases.data?.find((item) => item.release_id === activeReleaseId)
  const report = quality.data ?? release.data?.quality_report ?? release.data?.quality ?? selectedSummary?.quality_report ?? selectedSummary?.quality
  const account = paper.data?.account

  const healthPanel = health.isLoading
    ? <LoadingState />
    : health.error
      ? <ErrorState error={health.error} />
      : health.data
        ? <section className="panel health-panel"><div className="section-heading"><h2>运行环境</h2><div><span>{time(health.data.checked_at)}</span> <StatusBadge status={health.data.status} /></div></div><div className="health-grid">{health.data.checks.map((check) => <div key={check.name}><HealthIcon ok={check.ok} /><span><strong>{check.name}</strong><small>{check.detail}</small></span></div>)}</div></section>
        : null

  return <>
    <PageHeader title="数据基础层" />
    {healthPanel}

    <section className="panel pool-summary" aria-label="美股 PIT release">
      <div className="section-heading">
        <h2>美股 PIT Release</h2>
        <div>
          <span>sp500_ivv_proxy_v1</span>
          {releases.data?.length ? <select aria-label="PIT release" value={activeReleaseId} onChange={(event) => setSelectedReleaseId(event.target.value)}>
            {releases.data.map((item) => <option key={item.release_id} value={item.release_id}>{item.status} · {compactId(item.release_id)}</option>)}
          </select> : null}
        </div>
      </div>
      {releases.isLoading ? <LoadingState /> : releases.error ? <ErrorState error={releases.error} /> : !releases.data?.length ? <EmptyState>尚无 PIT release；严格美股回测保持 DATA_BLOCKED</EmptyState> : <>
        <div className="pool-facts">
          <div><span>质量状态</span><strong><StatusBadge status={report?.status ?? selectedSummary?.status ?? 'DATA_BLOCKED'} /></strong></div>
          <div><span>认证区间</span><strong>{date(selectedSummary?.certified_start)} — {date(selectedSummary?.certified_end)}</strong></div>
          <div><span>退市覆盖</span><strong>{report?.includes_delisted ?? selectedSummary?.includes_delisted ? '已验证' : '未验证'}</strong></div>
          <div><span>Release ID</span><strong className="hash-value" title={activeReleaseId}>{compactId(activeReleaseId)}</strong></div>
          <div><span>质量策略</span><strong>{report?.policy_version ?? selectedSummary?.quality_policy_version ?? '-'}</strong></div>
          <div><span>不可变制品</span><strong>{selectedSummary?.artifact_count ?? Object.keys(release.data?.artifacts ?? {}).length}</strong></div>
          <div><span>证据来源</span><strong>{selectedSummary?.source_count ?? release.data?.sources?.length ?? 0}</strong></div>
          <div><span>创建时间</span><strong>{time(selectedSummary?.created_at ?? release.data?.created_at)}</strong></div>
        </div>
        {(release.isLoading || quality.isLoading) && <LoadingState />}
        {(release.error || quality.error) && <div className="form-error">Release 详情读取失败：{release.error?.message ?? quality.error?.message}</div>}
      </>}
    </section>

    {report?.issues?.length ? <section className="table-section" aria-label="PIT 数据质量问题">
      <div className="section-heading"><h2>质量问题</h2><span>Critical / High 会阻断正式运行</span></div>
      <div className="table-wrap"><table><thead><tr><th>级别</th><th>代码</th><th>数据集</th><th>说明</th></tr></thead><tbody>{report.issues.map((issue, index) => <tr key={`${issue.code}-${index}`}><td><StatusBadge status={issue.severity} /></td><td className="symbol">{issue.code}</td><td>{issue.dataset}</td><td>{issue.message}</td></tr>)}</tbody></table></div>
    </section> : null}

    <section className="panel pool-summary" aria-label="美股自动模拟盘状态">
      <div className="section-heading"><h2>美股自动模拟盘</h2><div><span>只读 · 永不连接真实订单</span>{paper.data && <StatusBadge status={account?.status ?? paper.data.status ?? 'UNAVAILABLE'} />}</div></div>
      {paper.isLoading ? <LoadingState /> : paper.error ? <ErrorState error={paper.error} /> : paper.data ? <div className="pool-facts">
        <div><span>运行模式</span><strong>{paper.data.paper_only ? 'PAPER ONLY' : '安全门禁失败'}</strong></div>
        <div><span>账户状态</span><strong>{account?.status ?? paper.data.status ?? '-'}</strong></div>
        <div><span>现金</span><strong>{usd(account?.cash)}</strong></div>
        <div><span>持仓数</span><strong>{paper.data.positions.length}</strong></div>
        <div><span>部署门禁</span><strong><StatusBadge status={usPaperGateStatus(paper.data.deployment_gate)} /></strong></div>
        <div><span>观察资格</span><strong><StatusBadge status={paper.data.program?.state ?? usPaperGateStatus(paper.data.qualification)} /></strong></div>
        <div><span>待处理订单</span><strong>{paper.data.orders.filter((order) => !['FILLED', 'CANCELLED', 'EXPIRED', 'SKIPPED'].includes(String(order.status))).length}</strong></div>
        <div><span>最近更新</span><strong>{time(account?.updated_at)}</strong></div>
      </div> : null}
      {paper.data?.qualification_detail ? <div className="subtle-note">{paper.data.qualification_detail}</div> : null}
      {account?.degraded_reason ? <div className="form-error">{account.degraded_reason}</div> : null}
    </section>

    <section className="table-section"><div className="section-heading"><h2>权威数据源</h2><span>禁止字段级静默回退</span></div>{sources.isLoading ? <LoadingState /> : sources.error ? <ErrorState error={sources.error} /> : <div className="table-wrap"><table><thead><tr><th>数据集</th><th>Provider</th><th>缓存</th><th>用途</th></tr></thead><tbody>{sources.data?.map((source) => <tr key={String(source.dataset)}><td className="symbol">{String(source.dataset)}</td><td>{String(source.provider)}</td><td>{source.cacheable ? 'Parquet' : '-'}</td><td>{String(source.description)}</td></tr>)}</tbody></table></div>}</section>
  </>
}
