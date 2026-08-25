import { useQuery } from '@tanstack/react-query'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, money, time } from '../components'

const strategyNames: Record<string, string> = {
  course49_v10: '49课 V10（留出目标否决）', course49_v11: '49课 V11（历史稳健性否决）',
  chan_v1: '缠论', course49_v1: '49课 V1', course49_v2: '49课 V2', course49_v3: '49课 V3',
  course49_v4: '49课 V4', course49_v5: '49课 V5', course49_v6: '49课 V6',
  course49_v7: '49课 V7', course49_v8: '49课 V8', course49_v9: '49课 V9（留出否决）', pairs_arbitrage_v1: '配对套利',
  weekly_triangle_v1: '周线收敛三角形',
  weekly_bull_platform_v1: '周线底部平台多头',
}

export default function PortfolioPage() {
  const portfolio = useQuery({ queryKey: ['portfolio'], queryFn: api.portfolio, refetchInterval: 20_000 })
  if (portfolio.isLoading) return <LoadingState />
  if (portfolio.error) return <ErrorState error={portfolio.error} />
  const data = portfolio.data!
  return <>
    <PageHeader title="模拟组合" />
    <section className="metrics-band metrics-band--compact">
      {data.accounts.map((account) => {
        const positions = data.positions.filter((item) => item.strategy_id === account.strategy_id)
        const groupPositions = data.group_positions.filter((item) => item.strategy_id === account.strategy_id)
        const equity = account.cash + positions.reduce((sum, item) => sum + item.quantity * item.last_price, 0) + groupPositions.reduce((sum, item) => sum + item.quantity * item.last_price * (item.side === 'LONG' ? 1 : -1), 0)
        return <div key={account.strategy_id}><span>{strategyNames[account.strategy_id] ?? account.strategy_id}</span><strong>{money(equity)}</strong></div>
      })}
      <div><span>待成交订单</span><strong>{data.orders.filter((item) => item.status === 'PENDING').length + data.order_groups.filter((item) => item.status === 'APPROVED').length}</strong></div>
    </section>
    <section className="table-section"><div className="section-heading"><h2>套利组合持仓</h2><span>{new Set(data.group_positions.map((item) => `${item.strategy_id}:${item.group_key}`)).size} 组</span></div>{data.group_positions.length ? <div className="table-wrap"><table className="order-group-table"><thead><tr><th>策略</th><th>组合</th><th>代码</th><th>方向</th><th>入场</th><th className="numeric">数量</th><th className="numeric">成本</th><th className="numeric">现价</th><th className="numeric">浮动</th></tr></thead><tbody>{data.group_positions.map((item) => { const pnl = (item.last_price - item.average_price) * item.quantity * (item.side === 'LONG' ? 1 : -1); return <tr key={`${item.strategy_id}-${item.group_key}-${item.code}`}><td>{strategyNames[item.strategy_id] ?? item.strategy_id}</td><td className="symbol">{item.group_key}</td><td className="symbol">{item.code}</td><td className={item.side === 'LONG' ? 'positive-text' : 'negative-text'}>{item.side}</td><td>{time(item.entry_time)}</td><td className="numeric">{item.quantity}</td><td className="numeric">{item.average_price.toFixed(2)}</td><td className="numeric">{item.last_price.toFixed(2)}</td><td className={`numeric ${pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money(pnl)}</td></tr>})}</tbody></table></div> : <EmptyState />}</section>
    <section className="table-section"><div className="section-heading"><h2>多腿成交</h2><span>{data.group_fills.length} 条</span></div>{data.group_fills.length ? <div className="table-wrap"><table className="order-group-table"><thead><tr><th>时间</th><th>组合</th><th>代码</th><th>动作</th><th>方向</th><th className="numeric">数量</th><th className="numeric">价格</th><th className="numeric">费用</th><th className="numeric">损益</th></tr></thead><tbody>{data.group_fills.map((item) => <tr key={item.fill_id}><td>{time(item.timestamp)}</td><td className="symbol">{item.group_key}</td><td className="symbol">{item.code}</td><td>{item.action}</td><td>{item.side}</td><td className="numeric">{item.quantity}</td><td className="numeric">{item.price.toFixed(2)}</td><td className="numeric">{item.fees.toFixed(2)}</td><td className={`numeric ${(item.pnl ?? 0) >= 0 ? 'positive-text' : 'negative-text'}`}>{item.pnl == null ? '-' : money(item.pnl)}</td></tr>)}</tbody></table></div> : <EmptyState />}</section>
    <section className="table-section"><div className="section-heading"><h2>持仓明细</h2><span>{data.positions.length} 条</span></div>{data.positions.length ? <div className="table-wrap"><table><thead><tr><th>策略</th><th>代码</th><th>入场</th><th className="numeric">数量</th><th className="numeric">成本</th><th className="numeric">市值</th><th className="numeric">浮动</th></tr></thead><tbody>{data.positions.map((item) => { const pnl = (item.last_price - item.average_price) * item.quantity; return <tr key={`${item.strategy_id}-${item.code}`}><td>{item.strategy_id}</td><td className="symbol">{item.code}</td><td>{time(item.entry_time)}</td><td className="numeric">{item.quantity}</td><td className="numeric">{item.average_price.toFixed(2)}</td><td className="numeric">{money(item.last_price * item.quantity)}</td><td className={`numeric ${pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money(pnl)}</td></tr>})}</tbody></table></div> : <EmptyState />}</section>
    <section className="table-section"><div className="section-heading"><h2>最近成交</h2><span>{data.fills.length} 条</span></div>{data.fills.length ? <div className="table-wrap"><table><thead><tr><th>时间</th><th>策略</th><th>代码</th><th>方向</th><th className="numeric">数量</th><th className="numeric">价格</th><th className="numeric">费用</th><th className="numeric">损益</th></tr></thead><tbody>{data.fills.map((item) => <tr key={item.fill_id}><td>{time(item.timestamp)}</td><td>{item.strategy_id}</td><td className="symbol">{item.code}</td><td>{item.side}</td><td className="numeric">{item.quantity}</td><td className="numeric">{item.price.toFixed(2)}</td><td className="numeric">{item.fees.toFixed(2)}</td><td className="numeric">{item.pnl === null ? '-' : money(item.pnl)}</td></tr>)}</tbody></table></div> : <EmptyState />}</section>
  </>
}
