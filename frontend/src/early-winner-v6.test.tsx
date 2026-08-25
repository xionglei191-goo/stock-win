// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import EarlyWinnerV6Page from './pages/EarlyWinnerV6Page'
import StrategiesPage from './pages/StrategiesPage'

const mocks = vi.hoisted(() => ({
  earlyWinnerV6: vi.fn(),
  strategyCatalog: vi.fn(),
  orderGroups: vi.fn(),
}))

const hashes = {
  protocol: 'a'.repeat(64),
  evaluator: 'b'.repeat(64),
  labels: 'c'.repeat(64),
  dependencies: 'd'.repeat(64),
}

const project = {
  project_id: 'early_winner_v6',
  version: '6.0.0-preregistered',
  name: '早期强势股识别 V6 · 密封事件验证',
  description: '只读密封研究项目',
  category: 'research_project',
  lifecycle: 'RESEARCH_ONLY',
  status: 'BLOCKED_DATA',
  data_asof: null,
  data_gates: { preregistration: { ready: true, status: 'READY' } },
  strategy: { strategy_id: 'early_winner_event_quiet_v6', version: '6.0.0-preregistered', name: 'V6', lifecycle: 'RESEARCH_ONLY', category: 'research_project', scan_enabled: false, backtest_enabled: false },
  protocol: {
    protocol_version: 'early-winner-v6-sealed-event-v1',
    lifecycle: 'RESEARCH_ONLY',
    candidate_rule: { required_event_score: '>0', hard_negative: 'EXCLUDE', sort: [], industry_maximum: 5, portfolio_size: 20, unfilled_slot: 'CASH_NO_REFILL' },
    frozen_open: {
      manifest_version: 'early-winner-v6-frozen-manifest-v1', formats: ['parquet'], path_policy: 'FIXED_RUNTIME_ROOT_NO_REPARSE',
      per_shard_binding: ['year', 'relative_path', 'content_hash', 'schema_hash', 'row_count'],
      database_state_machine: ['SEALED', 'CONSUMING', 'RESULT_COMMITTED', 'FAILED_CLOSED'], one_open_only: true,
    },
    event_provenance: {},
    dependency_lock: { evaluator_bundle_hash: hashes.evaluator, label_schema_hash: hashes.labels, dependency_lock_hash: hashes.dependencies },
    assessment: { result_artifact: 'CONTENT_ADDRESSED_CANONICAL_JSON_ATOMIC_RENAME', ranking_metrics_source: 'ROW_LEVEL', portfolio_metrics_source: 'CYCLE_LEVEL', any_change_requires: 'V7' },
  },
  protocol_hash: hashes.protocol,
  design_years: [2018, 2019, 2020, 2021, 2022, 2023],
  frozen_validation_years: [2024, 2025], observation_years: [2026],
  historical_universe_master: { ready: false, status: 'SOURCE_INCOMPLETE', detail: '历史母表尚未覆盖至 2025。', coverage_start: '2018-01-01', coverage_end: '2023-12-31', promotion_blocked: true },
  frozen_open_state: 'NOT_SEALED', frozen_validation_opened: false,
  candidate_generation_enabled: false, trade_signals_enabled: false, promotion_allowed: false,
  v5_disposition: {
    status: 'PREREGISTRATION_REJECTED', superseded_by: 'early_winner_v6', v5_protocol_results_immutable: true,
    reasons: ['NO_FROZEN_YEAR_EVALUATION_ENTRY', 'MANIFEST_SHARDS_NOT_CONTENT_BOUND', 'NO_DATABASE_ATOMIC_ONE_TIME_CONSUME', 'ASSESSMENT_NOT_BOUND_TO_SNAPSHOT_AUDIT_RESULT'],
  },
}

vi.mock('./api', () => ({
  api: {
    earlyWinnerV6: mocks.earlyWinnerV6,
    strategyCatalog: mocks.strategyCatalog,
    orderGroups: mocks.orderGroups,
  },
}))

function provider(children: React.ReactNode) {
  return <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>{children}</QueryClientProvider>
}

describe('EarlyWinnerV6Page', () => {
  beforeEach(() => {
    cleanup()
    vi.clearAllMocks()
    mocks.earlyWinnerV6.mockResolvedValue(project)
  })

  it('shows only the sealed protocol and evidence status', async () => {
    render(provider(<EarlyWinnerV6Page />))

    expect(await screen.findByText('早期强势股识别 V6 · 密封事件验证')).toBeInTheDocument()
    expect(screen.getAllByText('NOT_SEALED').length).toBeGreaterThan(0)
    expect(screen.getByText('2024/2025')).toBeInTheDocument()
    expect(screen.getByText('V5：PREREGISTRATION_REJECTED')).toBeInTheDocument()
    for (const reason of project.v5_disposition.reasons) expect(screen.getByText(reason)).toBeInTheDocument()
    for (const hash of Object.values(hashes)) expect(screen.getByText(hash)).toBeInTheDocument()
    expect(screen.getByText('SEALED → CONSUMING → RESULT_COMMITTED → FAILED_CLOSED')).toBeInTheDocument()
    expect(screen.getByText('结果 CAS')).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
    expect(screen.queryByText(/开启验证|运行验证|训练模型|交易审批/)).not.toBeInTheDocument()
    expect(mocks.earlyWinnerV6).toHaveBeenCalledTimes(1)
  })
})

describe('V6 strategy catalog entry', () => {
  it('links the catalog-only strategy to its read-only detail page', async () => {
    mocks.strategyCatalog.mockResolvedValue({
      strategies: [{
        strategy_id: 'early_winner_event_quiet_v6', version: '6.0.0-preregistered', name: '早期强势股识别 V6', description: '密封研究项目',
        category: 'research_project', lifecycle: 'RESEARCH_ONLY', strategy_family: 'early_winner_v6', execution_model: 'SINGLE_LEG',
      }],
      groups: [], plugin_issues: [], archived_strategies: [],
    })
    mocks.orderGroups.mockResolvedValue([])

    render(<MemoryRouter>{provider(<StrategiesPage />)}</MemoryRouter>)

    expect(await screen.findByRole('link', { name: '打开 V6 密封验证' })).toHaveAttribute('href', '/research/early-winner-v6')
  })
})
