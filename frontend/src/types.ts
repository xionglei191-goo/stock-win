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

export type BacktestUniverse = 'all_a' | 'main_board' | 'growth' | 'star' | 'beijing' | 'all_us' | 'sp500_ivv_proxy_v1' | 'custom'
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
  pit_release_id?: string
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
  execution_funnel?: ExecutionFunnel
}

export type ExecutionFunnel = {
  generated_buy_signals: number
  attempted_next_open: number
  filled_buy_orders: number
  blocked_limit_up_open: number
  blocked_open_gap: number
  blocked_portfolio: number
  blocked_insufficient_cash: number
  blocked_missing_bars: number
  fill_rate: number
  by_playbook: Record<string, Omit<ExecutionFunnel, 'by_playbook'>>
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

export type USPITQualityIssue = {
  code: string
  severity: 'CRITICAL' | 'HIGH' | 'MEDIUM' | 'LOW'
  dataset: string
  message: string
  evidence: Record<string, unknown>
}

export type USPITQualityReport = {
  policy_version: string
  status: 'DATA_READY' | 'DATA_BLOCKED'
  includes_delisted: boolean
  issues: USPITQualityIssue[]
  metrics: Record<string, unknown>
}

export type USPITReleaseSummary = {
  release_id: string
  universe_id: 'sp500_ivv_proxy_v1'
  created_at: string
  status: 'DATA_READY' | 'DATA_BLOCKED'
  includes_delisted: boolean
  quality_policy_version: string
  artifact_count: number
  source_count: number
  certified_start?: string | null
  certified_end?: string | null
  metrics?: Record<string, unknown>
  quality?: USPITQualityReport
  quality_report?: USPITQualityReport
}

export type USPITArtifact = {
  name: string
  filename: string
  object_sha256: string
  size_bytes: number
  media_type: string
  row_count?: number | null
  schema_sha256?: string | null
}

export type USPITSource = {
  source_id: string
  source_version: string
  role: string
  license_class: string
  object_sha256: string
  observed_at: string
  url: string
  dataset: string
  as_of_date?: string | null
  published_at?: string | null
  metadata?: Record<string, unknown>
}

export type USPITReleaseDetail = USPITReleaseSummary & {
  format_version: string
  artifacts: Record<string, USPITArtifact>
  sources: USPITSource[]
  metadata: Record<string, unknown>
}

export type USPaperAccount = {
  account_id?: string
  status: string
  initial_cash?: number
  cash?: number
  updated_at?: string
  degraded_reason?: string
  killed_at?: string | null
  kill_reason?: string
  [key: string]: unknown
}

export type USPaperGate = {
  status?: string
  detail?: string
  reason?: string
  [key: string]: unknown
}

export type USPaperStatus = {
  mode: 'PAPER'
  paper_only: true
  status?: string
  account: USPaperAccount
  periods?: Array<Record<string, unknown>>
  orders: Array<Record<string, unknown>>
  positions: Array<Record<string, unknown>>
  fills: Array<Record<string, unknown>>
  events: Array<Record<string, unknown>>
  deployment_gate?: string | USPaperGate
  qualification?: string | USPaperGate
  qualification_detail?: string
  broker_writes_enabled?: boolean
  program?: {
    state: 'DATA_BLOCKED' | 'DATA_READY' | 'BACKTEST_QUALIFIED' | 'PAPER_COLLECTING' | 'PAPER_QUALIFIED' | 'HISTORICAL_FAILED' | 'PAPER_BLOCKED'
    release_id?: string | null
    data_ready?: boolean
    historical_qualified?: boolean
    tdx_qualified?: boolean
    paper_qualified?: boolean
    broker_writes_enabled: false
    [key: string]: unknown
  }
  heartbeat?: Record<string, unknown> | null
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
  runtime_adapter: 'chan_daily' | 'course49_daily' | 'generic_daily' | 'us_strict'
  plugin_api_version: string
  plugin_origin: string
  framework_id: string
  policy_version: string
  archived: boolean
  category: 'independent' | 'research_project'
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
  category?: 'framework' | 'independent' | 'research_archive' | 'research_project'
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

export type EarlyWinnerCandidate = {
  candidate_id: string
  project_id: string
  strategy_id: string
  method: 'rule' | 'ml'
  asof: string
  code: string
  name: string
  industry: string
  rank: number
  score: number
  probability?: number | null
  factor: Record<string, number>
  gate: Record<string, boolean>
  evidence_refs: string[]
  snapshot_id: string
}

export type EarlyWinnerProject = {
  project_id: 'early_winner_v1'
  version: string
  name: string
  description: string
  category: 'research_project'
  lifecycle: 'RESEARCH_ONLY'
  status: 'DATA_BUILDING' | 'VALIDATING' | 'OBSERVATION_ONLY' | 'BLOCKED_DATA' | 'VALIDATION_REJECTED'
  data_asof?: string | null
  data_gates: Record<string, { ready?: boolean; status?: string; detail?: string; row_count?: number }>
  strategies: Array<Pick<StrategyPlugin, 'strategy_id' | 'version' | 'name' | 'lifecycle' | 'category' | 'scan_enabled' | 'backtest_enabled'>>
  latest_model: Record<string, unknown> | null
  latest_validation: Record<string, unknown> | null
  latest_batches: Array<Record<string, unknown>>
  history: {
    status: string
    artifact_status?: string
    evidence_retained?: boolean
    trust_policy?: {
      ready?: boolean
      status?: string
      version?: string
      reasons?: string[]
    }
  }
  stored_status?: string
  candidates: { rule: EarlyWinnerCandidate[]; ml: EarlyWinnerCandidate[] }
  overlap: string[]
  trade_signals_enabled: false
  tdx_push_enabled: false
  promotion_allowed: false
  write_actions_enabled: false
  candidate_generation_enabled: false
  artifacts_audit_only: true
}

export type EarlyWinnerV2DevelopmentAudit = {
  validation_id: string
  status: string
  snapshot_id: string
  ml_metrics: Record<string, unknown>
  baseline_metrics: Record<string, unknown>
  stress_metrics: Record<string, unknown>
  gates: Record<string, { yearly?: Record<string, boolean>; passed?: boolean }>
  error: string
}

export type EarlyWinnerV2Project = {
  project_id: 'early_winner_v2'
  version: string
  name: string
  description: string
  category: 'research_project'
  lifecycle: 'RESEARCH_ONLY'
  status: 'DEVELOPMENT_AUDIT_REQUIRED' | 'DEVELOPMENT_AUDITING' | 'DEVELOPMENT_REJECTED' | 'DEVELOPMENT_READY' | 'BLOCKED_DATA'
  data_asof?: string | null
  data_gates: Record<string, { ready?: boolean; status?: string; detail?: string; row_count?: number }>
  strategy: Pick<StrategyPlugin, 'strategy_id' | 'version' | 'name' | 'lifecycle' | 'category' | 'scan_enabled' | 'backtest_enabled'>
  development_years: number[]
  excluded_tuning_years: number[]
  forward_year: number
  forward_validation_opened: false
  candidate_generation_enabled: false
  trade_signals_enabled: false
  promotion_allowed: false
  latest_development_audit: EarlyWinnerV2DevelopmentAudit | null
  latest_batches: Array<Record<string, unknown>>
}

export type EarlyWinnerV3Project = Omit<EarlyWinnerV2Project,
  'project_id' | 'status' | 'forward_year' | 'forward_validation_opened'> & {
  project_id: 'early_winner_v3'
  status: 'DATA_BUILDING' | 'DEVELOPMENT_AUDIT_REQUIRED' | 'DEVELOPMENT_AUDITING' | 'DEVELOPMENT_REJECTED' | 'DEVELOPMENT_READY' | 'BLOCKED_DATA'
  frozen_validation_opened: false
}

export type EarlyWinnerV4DataGate = {
  ready?: boolean
  status?: string
  detail?: string
  row_count?: number
  promotion_blocked?: boolean
  source_dataset_count?: number
  required_source_dataset_count?: number
  source_datasets?: string[]
  missing_source_datasets?: string[]
  finding_counts?: Record<string, number>
  manifest_hash?: string
  report_hash?: string
}

export type EarlyWinnerV4Project = Omit<EarlyWinnerV3Project,
  'project_id' | 'status' | 'data_gates'> & {
  project_id: 'early_winner_v4'
  status: 'DATA_BUILDING' | 'DEVELOPMENT_AUDIT_REQUIRED' | 'DEVELOPMENT_AUDITING' | 'DEVELOPMENT_REJECTED' | 'DEVELOPMENT_READY' | 'BLOCKED_DATA'
  data_gates: Record<string, EarlyWinnerV4DataGate>
  protocol: {
    holding_trading_days: 40
    embargo_trading_days: 20
    market_breadth_threshold: number
    target_quantile: number
    target_requires_positive_return: boolean
    feature_set: string[]
    random_seed: number
  }
}

export type EarlyWinnerV5Project = {
  project_id: 'early_winner_v5'
  version: string
  name: string
  description: string
  category: 'research_project'
  lifecycle: 'RESEARCH_ONLY'
  status: 'BLOCKED_DATA' | 'DESIGN_ONLY' | 'INCONCLUSIVE_SAMPLE' | 'VALIDATION_REJECTED' | 'OBSERVATION_ONLY'
  data_asof?: string | null
  data_gates: Record<string, {
    ready?: boolean
    status?: string
    detail?: string
    protocol_version?: string
    protocol_hash?: string
    snapshot_hash?: string
  }>
  strategy: Pick<StrategyPlugin, 'strategy_id' | 'version' | 'name' | 'lifecycle' | 'category' | 'scan_enabled' | 'backtest_enabled'>
  protocol: {
    protocol_version: string
    candidate_rule: {
      selected_event_score_strictly_positive: boolean
      hard_negative_blocks_new_position: boolean
      sort: string[]
      portfolio_size: number
      maximum_per_industry: number
      unfilled_slots: 'CASH_NO_REFILL'
      rank_before_entry_executable: boolean
    }
    evaluation: {
      holding_trading_days: number
      non_overlap_phases: number
      baseline: string
      paired_cycle_policy: string
      cost_policy: string
      drawdown_policy: string
    }
    protocol_change_policy: 'ANY_CHANGE_REQUIRES_V6'
    promotion_allowed: false
  }
  protocol_hash: string
  design_years: number[]
  frozen_validation_years: number[]
  observation_years: number[]
  frozen_validation_opened: false
  candidate_generation_enabled: false
  trade_signals_enabled: false
  promotion_allowed: false
}

export type EarlyWinnerV6OpenState =
  | 'NOT_SEALED'
  | 'SEALED'
  | 'CONSUMING'
  | 'RESULT_COMMITTED'
  | 'FAILED_CLOSED'

export type EarlyWinnerV6Gate = {
  ready?: boolean
  status?: string
  detail?: string
  [key: string]: unknown
}

export type EarlyWinnerV6Project = {
  project_id: 'early_winner_v6'
  version: string
  name: string
  description: string
  category: 'research_project'
  lifecycle: 'RESEARCH_ONLY'
  status: string
  data_asof?: string | null
  data_gates: Record<string, EarlyWinnerV6Gate>
  strategy: Pick<StrategyPlugin, 'strategy_id' | 'version' | 'name' | 'lifecycle' | 'category' | 'scan_enabled' | 'backtest_enabled'>
  protocol: {
    protocol_version: string
    lifecycle: 'RESEARCH_ONLY'
    candidate_rule: {
      required_event_score: string
      hard_negative: string
      sort: string[]
      industry_maximum: number
      portfolio_size: number
      unfilled_slot: string
    }
    frozen_open: {
      manifest_version: string
      formats: string[]
      path_policy: string
      per_shard_binding: string[]
      database_state_machine: Array<Exclude<EarlyWinnerV6OpenState, 'NOT_SEALED'>>
      one_open_only: boolean
    }
    event_provenance: Record<string, unknown>
    dependency_lock: {
      evaluator_bundle_hash: string
      label_schema_hash: string
      dependency_lock_hash: string
      [key: string]: unknown
    }
    assessment: {
      result_artifact: string
      ranking_metrics_source: string
      portfolio_metrics_source: string
      any_change_requires: string
      [key: string]: unknown
    }
  }
  protocol_hash: string
  design_years: number[]
  frozen_validation_years: number[]
  observation_years: number[]
  historical_universe_master: EarlyWinnerV6Gate & {
    snapshot_id?: string
    manifest_hash?: string
    coverage_start?: string
    coverage_end?: string
    promotion_blocked?: boolean
  }
  frozen_open_state: EarlyWinnerV6OpenState
  frozen_validation_opened: boolean
  candidate_generation_enabled: false
  trade_signals_enabled: false
  promotion_allowed: false
  v5_disposition: {
    status: 'PREREGISTRATION_REJECTED'
    superseded_by: 'early_winner_v6'
    v5_protocol_results_immutable: true
    reasons: string[]
  }
}

export type TradingOrderIntent = {
  intent_id: string
  code: string
  name: string
  industry: string
  side: 'BUY' | 'SELL'
  reason: string
  target_weight: number
  requested_quantity: number
  limit_price?: number | null
  status: string
  automatic_risk_exit: number
  evidence: Record<string, unknown>
}

export type TradingOrderBatch = {
  batch_id: string
  rebalance_date: string
  execution_date: string
  mode: 'SHADOW' | 'LIVE' | 'LIVE_RISK'
  status: string
  champion_hash: string
  snapshot_id: string
  generated_at: string
  approval_deadline: string
  approved_at?: string | null
  decision_note: string
  intents?: TradingOrderIntent[]
}

export type TradingQualificationMetrics = {
  weeks: number
  fills: number
  exits: number
  execution_rate: number
  median_slippage: number
  p95_slippage: number
  data_success_rate?: number
  point_in_time_failures?: number
  duplicate_orders?: number
  unauthorized_orders?: number
  unresolved_reconciliations?: number
  passed: boolean
}

export type EarlyWinnerTradingDeployment = {
  deployment_id: string
  strategy_id: 'early_winner_trade_v1'
  project_id: 'early_winner_v1'
  state: string
  champion: Record<string, unknown>
  validation_id: string
  snapshot_id: string
  account_alias: string
  max_capital_cny?: number | null
  max_account_fraction?: number | null
  shadow_started_at?: string | null
  pilot_started_at?: string | null
  live_started_at?: string | null
  funding_complete: boolean
  live_write_enabled: boolean
  account_configured: boolean
  account_handle_persisted: false
  operator_token_configured: boolean
  scheduler_enabled: boolean
  next_rebalance_date: string
  latest_reconciliation: Record<string, unknown> | null
  risk_events: Array<Record<string, unknown>>
  order_batches: TradingOrderBatch[]
  qualification: {
    shadow: TradingQualificationMetrics
    live_pilot: TradingQualificationMetrics
  }
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
