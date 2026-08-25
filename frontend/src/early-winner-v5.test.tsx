// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EarlyWinnerV5Page from './pages/EarlyWinnerV5Page'
import StrategiesPage from './pages/StrategiesPage'

const mocks = vi.hoisted(() => ({
  earlyWinnerV5: vi.fn(),
  strategyCatalog: vi.fn(),
  orderGroups: vi.fn(),
}))

const project = {
  project_id: 'early_winner_v5', version: '5.0.0-preregistered', name: '早期强势股识别 V5 · 事件静默量价', description: '',
  category: 'research_project', lifecycle: 'RESEARCH_ONLY', status: 'BLOCKED_DATA', data_asof: null,
  data_gates: {
    historical_universe_master: { ready: false, status: 'SOURCE_INCOMPLETE', detail: '北交所退市与转板事件账不完整。' },
    preregistration: { ready: true, status: 'READY', protocol_version: 'early-winner-v5-event-quiet-v1', protocol_hash: 'a'.repeat(64) },
  },
  strategy: { strategy_id: 'early_winner_event_quiet_v5', version: '5.0.0-preregistered', name: 'V5', lifecycle: 'RESEARCH_ONLY', category: 'research_project', scan_enabled: false, backtest_enabled: false },
  protocol: {
    protocol_version: 'early-winner-v5-event-quiet-v1',
    candidate_rule: { selected_event_score_strictly_positive: true, hard_negative_blocks_new_position: true, sort: [], portfolio_size: 20, maximum_per_industry: 5, unfilled_slots: 'CASH_NO_REFILL', rank_before_entry_executable: true },
    evaluation: { holding_trading_days: 40, non_overlap_phases: 8, baseline: 'RS60', paired_cycle_policy: 'JOINT_LATEST_CAPITAL_AVAILABLE_BOUNDARY', cost_policy: '20BPS_ROUND_TRIP_PER_FILLED_SLOT; DOUBLE=40BPS', drawdown_policy: 'CYCLE_ENDPOINT_NAV_INCLUDING_INITIAL_1.0' },
    protocol_change_policy: 'ANY_CHANGE_REQUIRES_V6', promotion_allowed: false,
  },
  protocol_hash: 'a'.repeat(64), design_years: [2018, 2019, 2020, 2021, 2022, 2023], frozen_validation_years: [2024, 2025], observation_years: [2026],
  frozen_validation_opened: false, candidate_generation_enabled: false, trade_signals_enabled: false, promotion_allowed: false,
}

vi.mock('./api', () => ({
  api: {
    earlyWinnerV5: mocks.earlyWinnerV5,
    strategyCatalog: mocks.strategyCatalog,
    orderGroups: mocks.orderGroups,
  },
}))

function provider(children: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

describe('EarlyWinnerV5Page', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.earlyWinnerV5.mockResolvedValue(project)
  })

  it('shows the immutable preregistration and exposes no research or trading action', async () => {
    render(provider(<EarlyWinnerV5Page />))

    expect(await screen.findByText('早期强势股识别 V5 · 事件静默量价')).toBeInTheDocument()
    expect(screen.getByText('历史母表未通过，V5 只完成预注册')).toBeInTheDocument()
    expect(screen.getByText('2024/2025')).toBeInTheDocument()
    expect(screen.getByText('任何规则变更必须新建 V6')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByText(/训练模型|运行验证|交易审批/)).not.toBeInTheDocument()
  })
})

describe('V5 strategy catalog entry', () => {
  it('links the catalog-only project to the read-only detail page', async () => {
    mocks.strategyCatalog.mockResolvedValue({
      strategies: [{
        strategy_id: 'early_winner_event_quiet_v5', version: '5.0.0-preregistered', name: '早期强势股识别 V5', description: '研究项目',
        category: 'research_project', lifecycle: 'RESEARCH_ONLY', strategy_family: 'early_winner_v5', execution_model: 'SINGLE_LEG',
      }],
      groups: [], plugin_issues: [], archived_strategies: [],
    })
    mocks.orderGroups.mockResolvedValue([])

    render(<MemoryRouter>{provider(<StrategiesPage />)}</MemoryRouter>)

    expect(await screen.findByRole('link', { name: '打开 V5 预注册' })).toHaveAttribute('href', '/research/early-winner-v5')
  })
})
