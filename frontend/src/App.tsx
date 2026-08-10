import { lazy, Suspense } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Activity, BarChart3, BrainCircuit, Database, FlaskConical, LayoutDashboard, ListChecks, WalletCards } from 'lucide-react'
import { NavLink, Route, Routes } from 'react-router-dom'
import { api } from './api'
import { LoadingState, StatusBadge } from './components'

const BacktestsPage = lazy(() => import('./pages/BacktestsPage'))
const DataPage = lazy(() => import('./pages/DataPage'))
const OverviewPage = lazy(() => import('./pages/OverviewPage'))
const PortfolioPage = lazy(() => import('./pages/PortfolioPage'))
const ResearchPage = lazy(() => import('./pages/ResearchPage'))
const SignalsPage = lazy(() => import('./pages/SignalsPage'))
const StrategiesPage = lazy(() => import('./pages/StrategiesPage'))

const navigation = [
  { to: '/', label: '总览', icon: LayoutDashboard },
  { to: '/signals', label: '候选', icon: ListChecks },
  { to: '/research', label: '研究', icon: BrainCircuit },
  { to: '/strategies', label: '策略', icon: FlaskConical },
  { to: '/portfolio', label: '组合', icon: WalletCards },
  { to: '/backtests', label: '回测', icon: BarChart3 },
  { to: '/data', label: '数据', icon: Database },
]

export default function App() {
  const health = useQuery({ queryKey: ['health'], queryFn: api.health, refetchInterval: 30_000 })
  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">澄</span><span><strong>澄明投研</strong><small>TDX RESEARCH</small></span></div>
        <nav>
          {navigation.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} end={to === '/'} title={label}>
              <Icon size={19} /><span>{label}</span>
            </NavLink>
          ))}
        </nav>
        <div className="sidebar-footer"><Activity size={17} /><span>本机研究模式</span></div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <span className="market-label">多策略研究工作台</span>
          <div className="topbar-status"><span>数据底座</span><StatusBadge status={health.data?.status ?? 'CHECKING'} /></div>
        </header>
        <main>
          <Suspense fallback={<LoadingState />}>
            <Routes>
              <Route path="/" element={<OverviewPage />} />
              <Route path="/signals" element={<SignalsPage />} />
              <Route path="/research" element={<ResearchPage />} />
              <Route path="/strategies" element={<StrategiesPage />} />
              <Route path="/portfolio" element={<PortfolioPage />} />
              <Route path="/backtests" element={<BacktestsPage />} />
              <Route path="/data" element={<DataPage />} />
            </Routes>
          </Suspense>
        </main>
      </div>
    </div>
  )
}
