// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import DataPage, { usPaperGateStatus } from './pages/DataPage'

function response(payload: unknown) {
  return Promise.resolve({ ok: true, status: 200, json: async () => payload })
}

describe('US PIT and paper data status', () => {
  afterEach(() => vi.unstubAllGlobals())

  it('normalizes paper deployment gate states', () => {
    expect(usPaperGateStatus('PAPER_BLOCKED')).toBe('PAPER_BLOCKED')
    expect(usPaperGateStatus({ status: 'QUALIFIED' })).toBe('QUALIFIED')
    expect(usPaperGateStatus(undefined)).toBe('UNAVAILABLE')
  })

  it('renders immutable release quality and paper-only account state', async () => {
    const releaseId = 'a'.repeat(64)
    vi.stubGlobal('fetch', vi.fn((input: string | URL | Request) => {
      const path = String(input)
      if (path === '/api/health') return response({ status: 'READY', checked_at: '2026-08-12T12:00:00Z', checks: [] })
      if (path === '/api/sources') return response([])
      if (path === '/api/data/us-pit/releases') return response([{
        release_id: releaseId, universe_id: 'sp500_ivv_proxy_v1', created_at: '2026-08-12T12:00:00Z',
        status: 'DATA_READY', includes_delisted: true, quality_policy_version: 'us-pit-quality-v1',
        artifact_count: 12, source_count: 4, certified_start: '2020-01-31', certified_end: '2026-07-31',
      }])
      if (path === `/api/data/us-pit/releases/${releaseId}`) return response({
        release_id: releaseId, universe_id: 'sp500_ivv_proxy_v1', created_at: '2026-08-12T12:00:00Z',
        status: 'DATA_READY', includes_delisted: true, quality_policy_version: 'us-pit-quality-v1',
        artifact_count: 12, source_count: 4, format_version: 'us-pit-release-v1', artifacts: {}, sources: [], metadata: {},
      })
      if (path === `/api/data/us-pit/releases/${releaseId}/quality`) return response({
        policy_version: 'us-pit-quality-v1', status: 'DATA_READY', includes_delisted: true,
        issues: [{ code: 'ACTION_REVIEW', severity: 'LOW', dataset: 'corporate_actions', message: '已核验低风险提示', evidence: {} }],
        metrics: {},
      })
      if (path === '/api/us-paper/status') return response({
        mode: 'PAPER', paper_only: true,
        account: { status: 'PAPER_COLLECTING', cash: 95000, updated_at: '2026-08-12T12:00:00Z' },
        positions: [{ code: 'AAPL' }], orders: [], fills: [], events: [],
        deployment_gate: { status: 'QUALIFIED' }, qualification: { status: 'PAPER_COLLECTING' },
      })
      return Promise.reject(new Error(`unexpected request: ${path}`))
    }))

    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(<QueryClientProvider client={client}><DataPage /></QueryClientProvider>)

    await waitFor(() => expect(screen.getByText('已核验低风险提示')).toBeInTheDocument())
    expect(screen.getByText('PAPER ONLY')).toBeInTheDocument()
    expect(screen.getByText('已验证')).toBeInTheDocument()
    expect(screen.getByText('QUALIFIED')).toBeInTheDocument()
    expect(screen.getByText('2020-01-31 — 2026-07-31')).toBeInTheDocument()
  })
})
