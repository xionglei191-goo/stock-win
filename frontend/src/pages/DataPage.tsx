import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { ErrorState, HealthIcon, LoadingState, PageHeader, StatusBadge, time } from '../components'

export default function DataPage() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  const sources = useQuery({ queryKey: ['sources'], queryFn: api.sources })
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
    <section className="table-section"><div className="section-heading"><h2>权威数据源</h2><span>禁止字段级静默回退</span></div>{sources.isLoading ? <LoadingState /> : sources.error ? <ErrorState error={sources.error} /> : <div className="table-wrap"><table><thead><tr><th>数据集</th><th>Provider</th><th>缓存</th><th>用途</th></tr></thead><tbody>{sources.data?.map((source) => <tr key={String(source.dataset)}><td className="symbol">{String(source.dataset)}</td><td>{String(source.provider)}</td><td>{source.cacheable ? 'Parquet' : '-'}</td><td>{String(source.description)}</td></tr>)}</tbody></table></div>}</section>
  </>
}
