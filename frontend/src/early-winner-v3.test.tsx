// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import EarlyWinnerV3Page from './pages/EarlyWinnerV3Page'
import type { EarlyWinnerV3Project } from './types'

const project: EarlyWinnerV3Project = {
  project_id: 'early_winner_v3', version: '3.0.0-dev1', name: '早期强势股识别 V3',
  description: 'fixture', category: 'research_project', lifecycle: 'RESEARCH_ONLY',
  status: 'DEVELOPMENT_REJECTED', data_asof: '2023-12-31',
  data_gates: { frozen_2024_2025: { ready: false, status: 'SEALED', detail: '冻结测试期未打开' } },
  strategy: { strategy_id: 'early_winner_ml_v3', version: '3.0.0-dev1', name: 'V3', lifecycle: 'RESEARCH_ONLY', category: 'research_project', scan_enabled: false, backtest_enabled: false },
  development_years: [2018, 2019, 2020, 2021, 2022, 2023], excluded_tuning_years: [2024, 2025, 2026],
  frozen_validation_opened: false, candidate_generation_enabled: false, trade_signals_enabled: false, promotion_allowed: false,
  latest_development_audit: null, latest_batches: [],
}

describe('early winner V3 point-in-time repair page', () => {
  it('shows supplemental and development actions without validation or trading controls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => project }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><EarlyWinnerV3Page /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByText('早期强势股识别 V3')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /重建点时补数/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /运行开发审计/ })).toBeInTheDocument()
    expect(screen.getByText('冻结测试期未打开')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /冻结验证|交易|批准/ })).not.toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
