import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Play, Radio } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, money, time } from '../components'

export default function OverviewPage() {
  const queryClient = useQueryClient()
  const dashboard = useQuery({ queryKey: ['dashboard'], queryFn: api.dashboard, refetchInterval: 20_000 })
  const scan = useMutation({
    mutationFn: (push: boolean) => api.scan(push),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['jobs'] }),
  })
  if (dashboard.isLoading) return <LoadingState />
  if (dashboard.error) return <ErrorState error={dashboard.error} />
  const data = dashboard.data!
  const strategyName = Object.fromEntries(data.strategy_catalog.strategies.map((item) => [item.strategy_id, item.name]))
  const totalCash = data.accounts.reduce((sum, item) => sum + item.cash, 0)
  const marketValue = data.positions.reduce((sum, item) => sum + item.quantity * item.last_price, 0) + data.group_positions.reduce((sum, item) => sum + item.quantity * item.last_price, 0)
  return (
    <>
      <PageHeader title="市场与组合" actions={<>
        <button className="button button--secondary" onClick={() => scan.mutate(false)} disabled={scan.isPending} title="运行本地研究扫描"><Play size={17} />运行扫描</button>
        <button className="button" onClick={() => scan.mutate(true)} disabled={scan.isPending} title="扫描并同步通达信"><Radio size={17} />同步 TDX</button>
      </>} />
      <section className="metrics-band">
        <div><span>运行状态</span><strong><StatusBadge status={String(data.latest_run?.status ?? 'NOT_RUN')} /></strong></div>
        <div><span>可用现金</span><strong>{money(totalCash)}</strong></div>
        <div><span>持仓总敞口</span><strong>{money(marketValue)}</strong></div>
        <div><span>待确认</span><strong>{data.pending_signals}</strong></div>
      </section>
      <div className="two-column">
        <section className="panel">
          <div className="section-heading"><h2>策略子组合</h2><span>各 50% 预算</span></div>
          <div className="strategy-list">
            {data.accounts.map((account) => {
              const holdings = data.positions.filter((item) => item.strategy_id === account.strategy_id)
              const groupHoldings = data.group_positions.filter((item) => item.strategy_id === account.strategy_id)
              const equity = account.cash + holdings.reduce((sum, item) => sum + item.quantity * item.last_price, 0) + groupHoldings.reduce((sum, item) => sum + item.quantity * item.last_price * (item.side === 'LONG' ? 1 : -1), 0)
              return <div className="strategy-row" key={account.strategy_id}>
                <div><strong>{strategyName[account.strategy_id] ?? account.strategy_id}</strong><span>{holdings.length + new Set(groupHoldings.map((item) => item.group_key)).size} 个持仓</span></div>
                <div className="numeric"><strong>{money(equity)}</strong><span>现金 {money(account.cash)}</span></div>
              </div>
            })}
          </div>
        </section>
        <section className="panel">
          <div className="section-heading"><h2>最近运行</h2><span>{time(String(data.latest_run?.finished_at ?? ''))}</span></div>
          {data.latest_run ? <dl className="detail-list">
            <div><dt>类型</dt><dd>{String(data.latest_run.run_type)}</dd></div>
            <div><dt>模式</dt><dd>{String(data.latest_run.mode)}</dd></div>
            <div><dt>策略</dt><dd>{Array.isArray(data.latest_run.strategies) ? data.latest_run.strategies.join(' / ') : '-'}</dd></div>
            <div><dt>状态</dt><dd><StatusBadge status={String(data.latest_run.status)} /></dd></div>
          </dl> : <EmptyState />}
        </section>
      </div>
      <section className="table-section">
        <div className="section-heading"><h2>当前持仓</h2><span>{data.positions.length} 条</span></div>
        {data.positions.length ? <div className="table-wrap"><table><thead><tr><th>策略</th><th>代码</th><th className="numeric">数量</th><th className="numeric">成本</th><th className="numeric">现价</th><th className="numeric">止损</th></tr></thead><tbody>
          {data.positions.map((position) => <tr key={`${position.strategy_id}-${position.code}`}><td>{strategyName[position.strategy_id] ?? position.strategy_id}</td><td className="symbol">{position.code}</td><td className="numeric">{position.quantity}</td><td className="numeric">{position.average_price.toFixed(2)}</td><td className="numeric">{position.last_price.toFixed(2)}</td><td className="numeric negative-text">{position.stop_price.toFixed(2)}</td></tr>)}
        </tbody></table></div> : <EmptyState />}
      </section>
      {scan.error && <div className="toast toast--error">{scan.error.message}</div>}
      {scan.data && <div className="toast">任务已提交：{scan.data.job_id.slice(0, 8)}</div>}
    </>
  )
}
