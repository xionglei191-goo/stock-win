export type DoctorCheck = { name: string; ok: boolean; detail: string }
export type Doctor = { status: string; checked_at: string; checks: DoctorCheck[] }

export type Signal = {
  signal_id: string
  strategy_id: string
  generated_at: string
  code: string
  side: 'BUY' | 'SELL'
  strength: number
  target_weight: number
  valid_until: string
  stop_price: number | null
  status: string
  reason_codes: string[]
  evidence: Record<string, unknown>
  framework_id?: string
  playbook_id?: string
  policy_version?: string
  ai_review?: AiSignalReview | null
}

export type DecisionRequest = {
  decision: 'APPROVED' | 'REJECTED'
  note: string
  push_tdx: boolean
  reason_tags: string[]
  confidence: number
  max_acceptable_loss: number | null
  ai_review_id?: string
}

export type AiSignalReview = {
  review_id: string
  brief_id: string
  signal_id: string
  recommendation: 'SUPPORT' | 'OPPOSE' | 'INSUFFICIENT'
  confidence: number
  summary: string
  supporting: string[]
  opposing: string[]
  missing: string[]
  evidence_refs: string[]
}

export type EvidenceClaim = {
  text: string
  evidence_refs: string[]
  confidence: number
  limitations: string[]
}

export type ResearchBriefContent = {
  headline: string
  market_summary: EvidenceClaim
  strategy_summaries: EvidenceClaim[]
  portfolio_risks: EvidenceClaim[]
  data_gaps: EvidenceClaim[]
  caveats: string[]
  signal_reviews: AiSignalReview[]
}

export type ResearchBrief = {
  brief_id: string
  run_id: string
  status: string
  created_at: string
  generated_at?: string | null
  model?: string
  prompt_version: string
  input_hash: string
  response_id?: string
  content?: ResearchBriefContent
  usage?: Record<string, number>
  error: string
  reviews?: AiSignalReview[]
}

export type DecisionOutcome = {
  outcome_id: string
  strategy_id: string
  signal_id: string
  basis: 'ACTUAL' | 'COUNTERFACTUAL'
  status: string
  block_reason: string
  return_1d: number | null
  return_3d: number | null
  return_5d: number | null
  mae: number | null
  mfe: number | null
}

export type FeedbackAggregate = {
  strategy_id: string
  market_phase: string
  reason_tag: string
  ai_alignment: string
  sample_size: number
  sufficient_sample: boolean
  average_return_5d: number | null
  win_rate_5d: number | null
}

export type FeedbackSummary = { rows: DecisionOutcome[]; aggregates: FeedbackAggregate[] }

export type StrategyExperiment = {
  experiment_id: string
  base_strategy_id: string
  baseline_backtest_id: string
  hypothesis: string
  status: string
  created_at: string
  finished_at?: string | null
  model?: string
  summary?: string
  source_hash?: string
  validation?: { passed?: boolean; gates?: Record<string, boolean>; [key: string]: unknown }
  baseline_metrics?: BacktestMetrics
  candidate_metrics?: BacktestMetrics
  stress_metrics?: BacktestMetrics
  promoted_strategy_id?: string | null
  error: string
}

export type Account = {
  strategy_id: string
  initial_cash: number
  cash: number
  updated_at: string
  frozen?: number
}

export type Position = {
  strategy_id: string
  code: string
  quantity: number
  average_price: number
  entry_time: string
  stop_price: number
  last_price: number
}

export type Fill = {
  fill_id: string
  strategy_id: string
  code: string
  side: string
  timestamp: string
  quantity: number
  price: number
  fees: number
  pnl: number | null
}

export type Portfolio = {
  accounts: Account[]
  positions: Position[]
  orders: Record<string, unknown>[]
  fills: Fill[]
  equity: Record<string, unknown>[]
  order_groups: OrderGroupIntent[]
  group_positions: GroupPosition[]
  group_fills: GroupFill[]
}

export type Dashboard = {
  doctor: Doctor
  latest_run: Record<string, unknown> | null
  pending_signals: number
  accounts: Account[]
  positions: Position[]
  group_positions: GroupPosition[]
  strategy_catalog: StrategyCatalog
  latest_backtest: Record<string, unknown> | null
}

export type Backtest = {
  backtest_id: string
  strategy_id: string
  status: string
  started_at: string
  finished_at?: string
  start_date?: string | null
  end_date?: string | null
  snapshot_id?: string | null
  parameters?: BacktestParameters
  metrics?: BacktestMetrics
  error?: string
}

export type BacktestUniverse = 'all_a' | 'main_board' | 'growth' | 'star' | 'beijing' | 'custom'
export type SamplingMode = 'full' | 'stratified'
export type StrategyId = string

export type BacktestRequest = {
  strategy_id: StrategyId
  start_date?: string
  end_date?: string
  daily_bars: number
  max_stocks?: number
  universe: BacktestUniverse
  stock_codes: string[]
  sampling_mode: SamplingMode
  sample_seed: number
  execution_cost_multiplier: number
  refresh_data: boolean
  playbook_ids?: string[]
}

export type BacktestReplayRequest = {
  source_backtest_id: string
  strategy_id?: string
  start_date?: string
  end_date?: string
  execution_cost_multiplier: number
}

export type BacktestParameters = BacktestRequest & {
  resolved_symbols?: number
  loaded_symbols?: number
  resolved_daily_bars?: number
  lhb_events?: number
  limit_behavior_events?: number
  market_activity_days?: number
  course49_candidate_symbols?: number
  stock_pool_hash?: string
  universe_distribution?: {
    total: number
    segments: Record<string, number>
    exchanges: Record<string, number>
  }
  benchmark_actual_codes?: Record<string, string | null>
  strategy_versions?: Record<string, string>
  sector_membership_quality?: string
  sector_membership_source?: string
  sector_membership_hash?: string
  data_cache_key?: string
  cache_status?: string
  cache_hit_type?: string
  source_snapshot_id?: string
  data_asof?: string
  worker_threads?: number
  peak_memory_bytes?: number
  effective_batch_sizes?: Record<string, unknown>
  stage_durations_seconds?: Record<string, number>
  data_plan?: Record<string, unknown>
}

export type Course49Attribution = {
  cohort: 'CAPITAL_AND_BOARD' | 'CAPITAL_ONLY' | 'BOARD_ONLY' | 'BASIC'
  entries: number
  closed: number
  wins: number
  win_rate: number
  total_pnl: number
  avg_pnl: number
}

export type BacktestMetrics = {
  initial_cash?: number
  final_equity?: number
  total_return?: number
  annualized_return?: number
  max_drawdown?: number
  sharpe_ratio?: number
  trades?: number
  win_rate?: number
  profit_factor?: number | null
  trading_days?: number
  closed_trades?: number
  validation?: {
    status: 'HISTORICAL_RETURN_TARGET_MET' | 'HISTORICAL_THRESHOLD_MET' | 'VERIFIED' | 'UNVERIFIED'
    target_verified: boolean
    historical_threshold_met?: boolean
    evidence_sufficient: boolean
    target_met: boolean
    minimum_trading_days: number
    minimum_closed_trades: number
    target_annualized_return: number
    reasons: string[]
  }
  course49_attribution?: Course49Attribution[]
  style_attribution?: AttributionRow[]
  trade_mode_attribution?: AttributionRow[]
  exit_reason_attribution?: ExitAttributionRow[]
  average_capital_invested?: number
  average_gross_exposure?: number
  average_net_exposure?: number
  pair_groups?: number
  completed_pair_groups?: number
  pair_win_rate?: number
  pair_total_pnl?: number
  atomic_execution?: boolean
  short_execution?: string
  runtime_adapter?: string
  execution_model?: 'SINGLE_LEG' | 'MULTI_LEG'
  components?: Record<string, BacktestMetrics>
  playbook_attribution?: AttributionRow[]
}

export type AttributionRow = {
  market_style?: string
  trade_mode?: string
  playbook_id?: string
  entries: number
  closed: number
  wins: number
  win_rate: number
  total_pnl: number
}

export type ExitAttributionRow = {
  reason: string
  count: number
  wins: number
  total_pnl: number
}

export type BacktestTrade = {
  strategy_id: string
  timestamp: string
  code: string
  side: 'BUY' | 'SELL' | 'SHORT' | 'COVER'
  quantity: number
  price: number
  fees: number
  pnl: number | null
  reason: string
  evidence: Record<string, unknown>
  group_key?: string
  leg_id?: string
  framework_id?: string
  playbook_id?: string
  policy_version?: string
}

export type BacktestState = {
  strategy_id: string
  timestamp: string
  market_phase: string
  market_style: string
  suitability: number
  trade_mode: string
  entry_allowed: number
  state: Record<string, unknown>
}

export type PositionChange = {
  timestamp: string
  strategy_id: string
  code: string
  group_key?: string
  side: 'BUY' | 'SELL' | 'SHORT' | 'COVER'
  quantity_change: number
  quantity_before: number
  quantity_after: number
  price: number
  trade_value: number
}

export type StrategyComparison = BacktestMetrics & { strategy_id: string }

export type BacktestDetail = Backtest & {
  equity: Array<{ strategy_id: string; timestamp: string; equity: number; cash: number; positions: number }>
  trades: BacktestTrade[]
  position_changes: PositionChange[]
  comparison: StrategyComparison[]
  states: BacktestState[]
  playbook_states: PlaybookState[]
  playbook_attribution?: AttributionRow[]
}

export type Job = {
  job_id: string
  job_type: string
  status: 'QUEUED' | 'RUNNING' | 'SUCCEEDED' | 'FAILED'
  created_at: string
  started_at?: string | null
  finished_at?: string | null
  error: string
  phase?: string
  progress?: number
  detail?: string
  cache_status?: string
  waiting_reason?: string
}

export type DataRequirement = {
  dataset: string
  frequency: string
  adjustment: string
  lookback: number
  required: boolean
  fields: string[]
  provider?: string
  cacheable?: boolean
  available?: boolean
}

export type StrategyPlugin = {
  strategy_id: string
  version: string
  name: string
  description: string
  frequency: string
  requires_approval: boolean
  enabled: boolean
  asset_classes: string[]
  execution_model: 'SINGLE_LEG' | 'MULTI_LEG'
  supports_short: boolean
  data_requirements: DataRequirement[]
  strategy_family: string
  lifecycle: string
  scan_enabled: boolean
  backtest_enabled: boolean
  runtime_adapter: 'chan_daily' | 'course49_daily' | 'generic_daily'
  plugin_api_version: string
  plugin_origin: string
  framework_id: string
  policy_version: string
  archived: boolean
}

export type StrategyGroupMember = {
  strategy_id: string
  weight: number
  role: 'alpha' | 'risk'
  priority: number
}

export type StrategyGroup = {
  group_id: string
  version: string
  name: string
  description: string
  composition_mode: 'capital_sleeves' | 'score_fusion' | 'intersection' | 'risk_overlay' | 'comparison'
  conflict_policy: 'risk_first' | 'net_score' | 'priority'
  enabled: boolean
  built_in: boolean
  scan_supported: boolean
  backtest_supported: boolean
  scan_block_reason: string
  backtest_block_reason: string
  members: StrategyGroupMember[]
  category?: 'framework' | 'independent' | 'research_archive'
}

export type StrategyGroupDraft = Omit<
  StrategyGroup,
  'built_in' | 'scan_supported' | 'backtest_supported' | 'scan_block_reason' | 'backtest_block_reason'
>

export type PluginLoadIssue = {
  plugin_id: string
  origin: string
  code: string
  message: string
}

export type StrategyCatalog = {
  strategies: StrategyPlugin[]
  archived_strategies: StrategyPlugin[]
  groups: StrategyGroup[]
  frameworks: StrategyFramework[]
  plugin_issues: PluginLoadIssue[]
}

export type PlaybookState = {
  timestamp: string
  playbook_id: string
  lifecycle: string
  admitted: number
  candidate_count: number
  routed_count: number
  budget: number
  blocked_reasons: string[]
  funnel?: Record<string, number>
}

export type StrategyPlaybook = {
  playbook_id: string
  framework_id: string
  version: string
  name: string
  description: string
  lifecycle: string
  base_weight: number
  market_phase: string
}

export type StrategyFramework = {
  framework_id: string
  version: string
  name: string
  description: string
  strategy_id: string
  policy_version: string
  enabled: number
  playbooks: StrategyPlaybook[]
}

export type Course49FrameworkDetail = StrategyFramework & {
  latest_run: Record<string, unknown> | null
  latest_backtest: Backtest | null
  state: Record<string, unknown>
  candidates: Array<Record<string, unknown>>
  signals: Signal[]
  positions: Array<Position & { evidence?: Record<string, unknown> }>
  runtime_states: Array<{ scope: string; asof: string; state: Record<string, unknown> }>
  history: BacktestState[]
  playbook_history: PlaybookState[]
}

export type OrderGroupLeg = {
  leg_id: string
  intent_id: string
  code: string
  side: 'BUY' | 'SELL' | 'SHORT' | 'COVER'
  ratio: number
  target_weight: number
}

export type OrderGroupIntent = {
  intent_id: string
  strategy_id: string
  generated_at: string
  valid_until: string
  group_key: string
  action: 'OPEN' | 'CLOSE' | 'REBALANCE'
  strength: number
  gross_target_weight: number
  status: string
  reason_codes: string[]
  evidence: Record<string, unknown>
  legs: OrderGroupLeg[]
}

export type GroupPosition = {
  strategy_id: string
  group_key: string
  code: string
  side: 'LONG' | 'SHORT'
  quantity: number
  average_price: number
  entry_time: string
  last_price: number
  ratio: number
  target_weight: number
}

export type GroupFill = {
  fill_id: string
  intent_id: string
  strategy_id: string
  group_key: string
  code: string
  side: string
  action: string
  timestamp: string
  quantity: number
  price: number
  fees: number
  pnl: number | null
}
