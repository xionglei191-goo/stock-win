// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EarlyWinnerV4Page from './pages/EarlyWinnerV4Page'
import StrategiesPage from './pages/StrategiesPage'

const mocks = vi.hoisted(() => ({
  earlyWinnerV4: vi.fn(),
  strategyCatalog: vi.fn(),
  orderGroups: vi.fn(),
}))

const rejectedProject = {
  project_id: 'early_winner_v4', version: '4.0.0-dev1', name: '早期强势股识别 V4', description: '', category: 'research_project', lifecycle: 'RESEARCH_ONLY', status: 'DEVELOPMENT_REJECTED',
  data_gates: { development_stability: { ready: false, status: 'REJECTED', detail: '至少一年未通过' }, frozen_2024_2025: { ready: false, status: 'SEALED' } },
  strategy: { strategy_id: 'early_winner_ml_v4', version: '4.0.0-dev1', name: 'V4', lifecycle: 'RESEARCH_ONLY', category: 'research_project', scan_enabled: false, backtest_enabled: false },
  development_years: [2018, 2019, 2020, 2021, 2022, 2023], excluded_tuning_years: [2024, 2025, 2026], frozen_validation_opened: false, candidate_generation_enabled: false, trade_signals_enabled: false, promotion_allowed: false,
  protocol: { holding_trading_days: 40, embargo_trading_days: 20, market_breadth_threshold: 0.5, target_quantile: 0.9, target_requires_positive_return: true, feature_set: [], random_seed: 49 }, latest_batches: [],
  latest_development_audit: { validation_id: 'audit', status: 'DEVELOPMENT_REJECTED', snapshot_id: 'snapshot', error: '', stress_metrics: {},
    ml_metrics: { yearly: { '2020': { precision_at_20: 0.09, total_return: 0.08, double_cost_return: 0.07, max_drawdown: -0.08, gate_passed: false } } },
    baseline_metrics: { yearly: { '2020': { precision_at_20: 0.10, total_return: -0.13, double_cost_return: -0.14, max_drawdown: -0.15 } } },
    gates: { market_conditioned_ml: { yearly: { '2020': false, '2021': false, '2022': false, '2023': false }, passed: false } } },
}

vi.mock('./api', () => ({
  api: {
    earlyWinnerV4: mocks.earlyWinnerV4,
    strategyCatalog: mocks.strategyCatalog,
    orderGroups: mocks.orderGroups,
    buildLabelsEarlyWinnerV4: vi.fn(), auditEarlyWinnerV4: vi.fn(),
  },
}))

function renderV4() {
  return render(<QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><EarlyWinnerV4Page /></QueryClientProvider>)
}

describe('EarlyWinnerV4Page', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.earlyWinnerV4.mockResolvedValue(rejectedProject)
  })

  it('shows the rejected gate and keeps frozen periods sealed', async () => {
    renderV4()
    expect(await screen.findByText('早期强势股识别 V4')).toBeInTheDocument()
    expect(screen.getByText('2024/2025 封存')).toBeInTheDocument()
    expect(screen.getByText('V4 已被开发期否决，不能进入交易')).toBeInTheDocument()
    expect(screen.getAllByText('FAILED').length).toBeGreaterThan(0)
  })

  it('shows data building without claiming rejection and exposes only research actions', async () => {
    mocks.earlyWinnerV4.mockResolvedValue({
      ...rejectedProject,
      status: 'DATA_BUILDING',
      data_gates: { label_snapshot: { ready: false, status: 'BUILDING' }, frozen_2024_2025: { ready: false, status: 'SEALED' } },
      latest_development_audit: null,
    })

    renderV4()

    expect(await screen.findByText('V4 正在构建40日标签快照')).toBeInTheDocument()
    expect(screen.queryByText('V4 已被开发期否决，不能进入交易')).not.toBeInTheDocument()
    expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['重建40日标签', '运行开发审计'])
    expect(screen.queryByRole('button', { name: /验证|交易|审批/ })).not.toBeInTheDocument()
  })

  it('shows the historical-universe blocker before any frozen validation', async () => {
    mocks.earlyWinnerV4.mockResolvedValue({
      ...rejectedProject,
      status: 'BLOCKED_DATA',
      data_gates: {
        ...rejectedProject.data_gates,
        historical_universe_master: {
          ready: false,
          status: 'SURVIVORSHIP_BIAS_CONFIRMED',
          detail: '交易所对账发现 239 只历史证券未进入当前 TDX 母表。',
        },
      },
    })

    renderV4()

    expect(await screen.findByText('V4 已被数据门禁阻断')).toBeInTheDocument()
    expect(screen.getByText('历史证券母表未通过')).toBeInTheDocument()
    expect(screen.getAllByText('交易所对账发现 239 只历史证券未进入当前 TDX 母表。')).toHaveLength(2)
    expect(screen.getByText('2024/2025 封存')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /验证|交易|审批/ })).not.toBeInTheDocument()
  })

  it('shows the partial delisted-history audit and disables blocked actions', async () => {
    mocks.earlyWinnerV4.mockResolvedValue({
      ...rejectedProject,
      status: 'BLOCKED_DATA',
      data_gates: {
        frozen_2024_2025: { ready: false, status: 'SEALED' },
        historical_universe_master: { ready: true, status: 'READY' },
        delisted_history_quality: {
          ready: false,
          status: 'DELISTED_HISTORY_SOURCE_INCOMPLETE',
          source_dataset_count: 2,
          required_source_dataset_count: 12,
          source_datasets: ['raw_execution_bars', 'trading_calendar'],
          missing_source_datasets: ['adjusted_bars_factors', 'suspension_status'],
          finding_counts: { SOURCE_INDEX_MISSING: 10, RAW_BAR_MISSING_UNEXPLAINED: 233855 },
        },
      },
      latest_development_audit: null,
    })

    renderV4()

    expect(await screen.findByText('退市历史证据门禁')).toBeInTheDocument()
    expect(screen.getByText('2/12 类')).toBeInTheDocument()
    expect(screen.getByText((_, node) => node?.tagName === 'P' && node.textContent === '已验证：未复权执行行情、官方交易日历')).toBeInTheDocument()
    expect(screen.getByText((_, node) => node?.tagName === 'P' && node.textContent === '仍缺：前复权行情与因子、独立停复牌状态')).toBeInTheDocument()
    expect(screen.getByText('233,855')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '重建40日标签' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '运行开发审计' })).toBeDisabled()
    expect(screen.queryByText('历史证券母表未通过')).not.toBeInTheDocument()
  })
})

describe('V4 strategy catalog entry', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.strategyCatalog.mockResolvedValue({
      strategies: [{
        strategy_id: 'early_winner_ml_v4', version: '4.0.0-dev1', name: '早期强势股识别 V4', description: '研究项目',
        category: 'research_project', lifecycle: 'RESEARCH_ONLY', strategy_family: 'early_winner_v4', execution_model: 'SINGLE_LEG',
      }],
      groups: [], plugin_issues: [], archived_strategies: [],
    })
    mocks.orderGroups.mockResolvedValue([])
  })

  it('links the research catalog card to the V4 detail page', async () => {
    render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><StrategiesPage /></QueryClientProvider></MemoryRouter>)

    expect(await screen.findByRole('link', { name: '打开 V4 研究' })).toHaveAttribute('href', '/research/early-winner-v4')
  })
})
