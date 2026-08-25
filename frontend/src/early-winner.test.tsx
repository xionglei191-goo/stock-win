// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import EarlyWinnerPage from './pages/EarlyWinnerPage'
import type { EarlyWinnerProject } from './types'

const project: EarlyWinnerProject = {
  project_id: 'early_winner_v1',
  version: '1.0.0',
  name: '早期强势股识别',
  description: 'fixture',
  category: 'research_project',
  lifecycle: 'RESEARCH_ONLY',
  status: 'BLOCKED_DATA',
  data_asof: null,
  data_gates: {
    tdx_http: { ready: false, status: 'NOT_INSTALLED', detail: '未发现通达信' },
  },
  strategies: [],
  latest_model: null,
  latest_validation: null,
  latest_batches: [],
  history: {
    status: 'BLOCKED_DATA',
    artifact_status: 'SUCCEEDED',
    evidence_retained: true,
    trust_policy: {
      ready: false,
      status: 'SUPERSEDED_DATA_QUALITY_REJECTED',
      version: 'early-winner-legacy-history-quarantine-v1',
      reasons: ['survivorship bias'],
    },
  },
  candidates: { rule: [], ml: [] },
  overlap: [],
  trade_signals_enabled: false,
  tdx_push_enabled: false,
  promotion_allowed: false,
  write_actions_enabled: false,
  candidate_generation_enabled: false,
  artifacts_audit_only: true,
}

describe('early winner research page', () => {
  it('shows data gates and research actions without trade controls', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => project,
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><EarlyWinnerPage /></QueryClientProvider>)

    await waitFor(() => expect(screen.getByText('早期强势股识别')).toBeInTheDocument())
    expect(screen.getByRole('button', { name: /更新数据/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /训练 ML/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /运行验证/ })).toBeDisabled()
    expect(screen.getByText('V1 历史证据已撤销资格')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /批准|交易|推送/ })).not.toBeInTheDocument()
    expect(screen.getByText('未发现通达信')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
