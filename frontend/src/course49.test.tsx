// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import Course49Page from './pages/Course49Page'

describe('Course49 workbench', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('renders shared context, funnel and production playbooks', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({
        framework_id: 'course49',
        version: '1.0.0',
        name: '49课体系',
        description: 'shared context',
        strategy_id: 'course49_system',
        policy_version: '1.0.0',
        playbooks: [
          { playbook_id: 'recovery_ignition', framework_id: 'course49', version: '1.0.0', name: '修复启动', description: 'test', lifecycle: 'PRODUCTION', base_weight: 0.15, market_phase: 'RECOVERY', data_requirements: [] },
          { playbook_id: 'ferment_second_board', framework_id: 'course49', version: '1.0.0', name: '发酵二板', description: 'test', lifecycle: 'PRODUCTION', base_weight: 0.25, market_phase: 'FERMENT', data_requirements: [] },
          { playbook_id: 'acceleration_core_relay', framework_id: 'course49', version: '1.0.0', name: '加速核心接力', description: 'test', lifecycle: 'PRODUCTION', base_weight: 0.20, market_phase: 'ACCELERATION', data_requirements: [] },
        ],
        state: {
          market_phase: 'DIVERGENCE', market_style: 'BROAD_RISK_ON', style_suitability: 0.8,
          asof: '2026-08-07', context_version: '1.0.0', entry_allowed: false,
          entry_block_reason: 'market_ecology_not_entry_ready',
          data_completeness: { critical_benchmarks: true },
          funnel: { market: 5500, eligible: 5100, strong_themes: 3, leaders: 6, playbook_hits: 1, routed: 1 },
          playbook_states: [],
        },
        candidates: [], signals: [], positions: [], runtime_states: [], history: [],
        playbook_history: [], latest_run: null, latest_backtest: null,
      }),
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><Course49Page /></QueryClientProvider>)

    await waitFor(() => expect(screen.getByText('全面风险偏好')).toBeInTheDocument())
    expect(document.querySelector('.framework-block-reason')).toHaveTextContent('市场生态不适合开仓')
    expect(screen.getByText('5500')).toBeInTheDocument()
    expect(screen.getByText('修复启动')).toBeInTheDocument()
    expect(screen.getByText('发酵二板')).toBeInTheDocument()
    expect(screen.getByText('加速核心接力')).toBeInTheDocument()
  })
})
