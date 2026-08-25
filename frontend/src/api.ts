import type { Backtest, BacktestDetail, BacktestReplayRequest, BacktestRequest, Course49FrameworkDetail, Dashboard, DecisionRequest, Doctor, EarlyWinnerCandidate, EarlyWinnerProject, EarlyWinnerV2Project, EarlyWinnerV3Project, EarlyWinnerV4Project, EarlyWinnerV5Project, EarlyWinnerV6Project, FeedbackSummary, Job, OrderGroupIntent, Portfolio, ResearchBrief, Signal, StrategyCatalog, StrategyExperiment, StrategyFramework, StrategyGroup, StrategyGroupDraft, USPaperStatus, USPITQualityReport, USPITReleaseDetail, USPITReleaseSummary } from './types'

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(options?.headers ?? {}) },
    ...options,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(payload.detail ?? response.statusText)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export const api = {
  dashboard: () => request<Dashboard>('/api/dashboard'),
  health: () => request<Doctor>('/api/health'),
  signals: (status?: string) => request<Signal[]>(`/api/signals${status ? `?status=${status}` : ''}`),
  portfolio: () => request<Portfolio>('/api/portfolio'),
  backtests: () => request<Backtest[]>('/api/backtests'),
  backtest: (id: string) => request<BacktestDetail>(`/api/backtests/${id}`),
  sources: () => request<Array<Record<string, unknown>>>('/api/sources'),
  usPitReleases: async () => {
    const payload = await request<USPITReleaseSummary[] | { releases: USPITReleaseSummary[] }>('/api/data/us-pit/releases')
    return Array.isArray(payload) ? payload : payload.releases
  },
  usPitRelease: (id: string) => request<USPITReleaseDetail>(`/api/data/us-pit/releases/${encodeURIComponent(id)}`),
  usPitQuality: (id: string) => request<USPITQualityReport>(`/api/data/us-pit/releases/${encodeURIComponent(id)}/quality`),
  usPaperStatus: () => request<USPaperStatus>('/api/us-paper/status'),
  strategyCatalog: () => request<StrategyCatalog>('/api/strategy-catalog'),
  earlyWinner: () => request<EarlyWinnerProject>('/api/research/early-winner'),
  earlyWinnerV2: () => request<EarlyWinnerV2Project>('/api/research/early-winner-v2'),
  auditEarlyWinnerV2: () => request<{ job_id: string; status: string }>('/api/research/early-winner-v2/development-audit', { method: 'POST' }),
  earlyWinnerV3: () => request<EarlyWinnerV3Project>('/api/research/early-winner-v3'),
  supplementEarlyWinnerV3: () => request<{ job_id: string; status: string }>('/api/research/early-winner-v3/supplement', { method: 'POST' }),
  auditEarlyWinnerV3: () => request<{ job_id: string; status: string }>('/api/research/early-winner-v3/development-audit', { method: 'POST' }),
  earlyWinnerV4: () => request<EarlyWinnerV4Project>('/api/research/early-winner-v4'),
  buildLabelsEarlyWinnerV4: () => request<{ job_id: string; status: string }>('/api/research/early-winner-v4/build-labels', { method: 'POST' }),
  auditEarlyWinnerV4: () => request<{ job_id: string; status: string }>('/api/research/early-winner-v4/development-audit', { method: 'POST' }),
  earlyWinnerV5: () => request<EarlyWinnerV5Project>('/api/research/early-winner-v5'),
  earlyWinnerV6: () => request<EarlyWinnerV6Project>('/api/research/early-winner-v6'),
  earlyWinnerCandidates: (method?: 'rule' | 'ml', asof?: string) => {
    const query = new URLSearchParams()
    if (method) query.set('method', method)
    if (asof) query.set('asof', asof)
    const suffix = query.size ? `?${query.toString()}` : ''
    return request<EarlyWinnerCandidate[]>(`/api/research/early-winner/candidates${suffix}`)
  },
  refreshEarlyWinner: () => request<{ job_id: string; status: string }>('/api/research/early-winner/refresh', { method: 'POST' }),
  trainEarlyWinner: () => request<{ job_id: string; status: string }>('/api/research/early-winner/train', { method: 'POST' }),
  validateEarlyWinner: () => request<{ job_id: string; status: string }>('/api/research/early-winner/validate', { method: 'POST' }),
  earlyWinnerHistoryStatus: () => request<Record<string, unknown>>('/api/research/early-winner/history/status'),
  buildEarlyWinnerHistory: (startYear = 2018, endYear = 2025) => request<{ job_id: string; status: string }>('/api/research/early-winner/history/build', {
    method: 'POST',
    body: JSON.stringify({ start_year: startYear, end_year: endYear }),
  }),
  frameworks: () => request<StrategyFramework[]>('/api/frameworks'),
  framework: (id: string) => request<Course49FrameworkDetail>('/api/frameworks/' + id),
  reloadStrategyCatalog: () => request<StrategyCatalog>('/api/strategy-catalog/reload', {
    method: 'POST',
  }),
  saveStrategyGroup: (payload: StrategyGroupDraft) => request<StrategyGroup>('/api/strategy-groups', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  deleteStrategyGroup: (id: string) => request<void>(`/api/strategy-groups/${id}`, { method: 'DELETE' }),
  orderGroups: () => request<OrderGroupIntent[]>('/api/order-groups'),
  jobs: () => request<Job[]>('/api/jobs'),
  job: (id: string) => request<Job>(`/api/jobs/${id}`),
  scan: (pushTdx: boolean) => request<{ job_id: string }>('/api/runs/scan', {
    method: 'POST',
    body: JSON.stringify({ strategies: ['course49_system'], mode: 'research', push_tdx: pushTdx }),
  }),
  scanSelection: (strategies: string[], pushTdx = false) => request<{ job_id: string }>('/api/runs/scan', {
    method: 'POST',
    body: JSON.stringify({ strategies, mode: 'research', push_tdx: pushTdx }),
  }),
  runBacktest: (payload: BacktestRequest) => request<{ job_id: string; status: string }>('/api/backtests', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  replayBacktest: (payload: BacktestReplayRequest) => request<{ job_id: string; status: string }>('/api/backtests/replay', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  decide: (id: string, payload: DecisionRequest) => request<Signal>(`/api/signals/${id}/decision`, {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  decideOrderGroup: (id: string, decision: 'APPROVED' | 'REJECTED') => request<OrderGroupIntent>(`/api/order-groups/${id}/decision`, {
    method: 'POST',
    body: JSON.stringify({ decision, push_tdx: false }),
  }),
  runDailyResearch: () => request<{ job_id: string; status: string }>('/api/research/daily', {
    method: 'POST',
    body: JSON.stringify({ strategies: ['course49_system'], mode: 'research', push_tdx: false }),
  }),
  generateBrief: (runId: string) => request<{ job_id: string; status: string }>('/api/research/briefs', {
    method: 'POST',
    body: JSON.stringify({ run_id: runId }),
  }),
  researchBriefs: () => request<ResearchBrief[]>('/api/research/briefs'),
  researchBrief: (id: string) => request<ResearchBrief>(`/api/research/briefs/${id}`),
  refreshFeedback: () => request<{ job_id: string; status: string }>('/api/research/feedback/refresh', { method: 'POST' }),
  feedbackSummary: () => request<FeedbackSummary>('/api/research/feedback/summary'),
  experiments: () => request<StrategyExperiment[]>('/api/research/experiments'),
  createExperiment: (baselineBacktestId: string, hypothesis: string) => request<{ job_id: string; status: string }>('/api/research/experiments', {
    method: 'POST',
    body: JSON.stringify({ baseline_backtest_id: baselineBacktestId, hypothesis }),
  }),
  promoteExperiment: (id: string) => request<StrategyExperiment>(`/api/research/experiments/${id}/promote`, { method: 'POST' }),
}
