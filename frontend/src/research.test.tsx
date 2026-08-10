// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { BriefWorkspace } from './pages/ResearchPage'
import { DecisionDialog } from './pages/SignalsPage'
import type { ResearchBrief, Signal } from './types'

const signal: Signal = {
  signal_id: 'signal-1',
  strategy_id: 'course49_v3',
  generated_at: '2026-08-09T09:00:00+08:00',
  code: '600000.SH',
  side: 'BUY',
  strength: 0.8,
  target_weight: 0.2,
  valid_until: '2026-08-11T09:00:00+08:00',
  stop_price: 9,
  status: 'PROPOSED',
  reason_codes: ['TEST'],
  evidence: {},
}

describe('research workflow', () => {
  it('submits a structured approval with TDX push disabled by default', () => {
    const confirm = vi.fn()
    render(<DecisionDialog signal={signal} decision="APPROVED" pending={false} onClose={() => undefined} onConfirm={confirm} />)

    fireEvent.click(screen.getByLabelText('策略证据充分'))
    fireEvent.click(screen.getByRole('button', { name: '确认批准' }))

    expect(confirm).toHaveBeenCalledWith(expect.objectContaining({
      decision: 'APPROVED',
      reason_tags: ['策略证据充分'],
      confidence: 60,
      push_tdx: false,
    }))
  })

  it('shows a failed AI brief without hiding deterministic workflow status', async () => {
    const brief: ResearchBrief = {
      brief_id: 'brief-1', run_id: 'run-1', status: 'FAILED',
      created_at: '2026-08-09T09:00:00+08:00', prompt_version: 'v1',
      input_hash: 'abc', error: 'model timeout',
    }
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => brief,
    }))
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><BriefWorkspace briefs={[brief]} /></QueryClientProvider>)

    await waitFor(() => expect(screen.getByText('AI 简报不可用')).toBeInTheDocument())
    expect(screen.getByText('扫描、候选和模拟交易数据不受影响。')).toBeInTheDocument()
    vi.unstubAllGlobals()
  })
})
