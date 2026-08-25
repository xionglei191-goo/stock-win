// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import EarlyWinnerV2Page from './pages/EarlyWinnerV2Page'
import type { EarlyWinnerV2Project } from './types'

const project: EarlyWinnerV2Project = {
  project_id: 'early_winner_v2', version: '2.0.0-dev1', name: '早期强势股识别 V2',
  description: 'fixture', category: 'research_project', lifecycle: 'RESEARCH_ONLY',
  status: 'DEVELOPMENT_REJECTED', data_asof: '2023-12-31',
  data_gates: { forward_2026: { ready: false, status: 'SEALED', detail: '2026 前瞻集未打开' } },
  strategy: { strategy_id: 'early_winner_ml_v2', version: '2.0.0-dev1', name: 'V2', lifecycle: 'RESEARCH_ONLY', category: 'research_project', scan_enabled: false, backtest_enabled: false },
  development_years: [2018, 2019, 2020, 2021, 2022, 2023], excluded_tuning_years: [2024, 2025], forward_year: 2026,
  forward_validation_opened: false, candidate_generation_enabled: false, trade_signals_enabled: false, promotion_allowed: false,
  latest_development_audit: null, latest_batches: [],
}

describe('early winner V2 development page', () => {
  it('shows sealed forward data and no training or trading controls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, status: 200, json: async () => project }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><EarlyWinnerV2Page /></QueryClientProvider>)
    await waitFor(() => expect(screen.getByText('早期强势股识别 V2')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /运行开发期审计/ })).toBeInTheDocument()
    expect(screen.getByText('2026 前瞻集未打开')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /训练|验证|交易|批准/ })).not.toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
