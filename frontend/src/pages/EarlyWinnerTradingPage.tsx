import { ShieldX } from 'lucide-react'
import { PageHeader } from '../components'

export default function EarlyWinnerTradingPage() {
  return <>
    <PageHeader title="真实交易未编译" />
    <section className="panel">
      <div className="empty-state">
        <ShieldX size={32} />
        <strong>当前构建仅支持研究、严格回测和自动模拟盘</strong>
        <p>前端没有真实券商查询、批准、下单、撤单或恢复入口。</p>
      </div>
    </section>
  </>
}
