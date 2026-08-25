import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical } from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, time } from '../components'
import type { AttributionRow, BacktestRequest, BacktestState, BacktestUniverse, SamplingMode } from '../types'

const fallbackStrategyNames: Record<string, string> = {
  course49_system: '49课体系',
  course49_system_compare: '49课 V2 / 体系影子对比',
  course49_v10: '49课 V10 市场奖励过滤（留出目标否决）',
  course49_v11: '49课 V11 三次开板回封（历史稳健性否决）',
  course49_v10_compare: '49课 V9 / V10 市场奖励过滤对比',
  course49_v11_compare: '49课 V9 / V11 回封强度对比',
  combined: '缠论 / 49课历史比较组合',
  chan_v1: '缠论结构（历史否决）',
  course49_v1: '49课 V1 基线',
  course49_v2: '49课 V2 自适应',
  course49_v3: '49课 V3 局部加速',
  course49_v4: '49课 V4 资金确认加速',
  course49_v5: '49课 V5 资金确认风险预算',
  course49_v6: '49课 V6 小盘加速首板',
  course49_v7: '49课 V7 全面风险偏好首板回封',
  course49_v8: '49课 V8 低拥挤首板回封',
  course49_v9: '49课 V9 低拥挤回封（留出否决）',
  course49_compare: '49课 V1 / V2 对比',
  course49_v3_compare: '49课 V2 / V3 对比',
  course49_v4_compare: '49课 V3 / V4 对比',
  course49_v5_compare: '49课 V4 / V5 风险预算对比',
  course49_v6_compare: '49课 V5 / V6 奖励切换对比',
  course49_v9_compare: '49课 V6 / V9 失效边界对比',
  pairs_arbitrage_v1: '配对套利 V1（历史否决）',
  weekly_triangle_v1: '周线均线聚合收敛三角形',
  weekly_bull_platform_v1: '周线底部平台多头',
  us_momentum_v1: '美股动量 V1（严格 PIT）',
  adaptive_multi_strategy: '多策略历史比较组合',
}

const rejectedLifecycleNames: Record<string, string> = {
  HISTORICAL_REJECTED: '历史留出否决',
  HOLDOUT_TARGET_REJECTED: '留出收益目标否决',
  HISTORICAL_ROBUSTNESS_REJECTED: '历史稳健性否决',
}

export function strategyCatalogDisplayName(name: string, lifecycle: string) {
  if (!lifecycle.endsWith('_REJECTED') || name.includes('否决')) return name
  return `${name}（${rejectedLifecycleNames[lifecycle] ?? '研究否决'}）`
}

export function backtestCostLabel(multiplier?: number | null) {
  const value = Number(multiplier ?? 1)
  const formatted = Number.isInteger(value) ? String(value) : value.toFixed(1)
  return value > 1 ? `${formatted} 倍成本` : '标准成本'
}

const universeNames: Record<BacktestUniverse, string> = {
  all_a: '全 A 股',
  main_board: '沪深主板',
  growth: '创业板',
  star: '科创板',
  beijing: '北交所',
  all_us: '全美股',
  sp500_ivv_proxy_v1: 'IVV PIT 标普 500 代理',
  custom: '自定义股票',
}

const lineColors: Record<string, string> = {
  course49_system: '#146c5a',
  course49_v10: '#6f5b3e',
  course49_v11: '#267067',
  chan_v1: '#146c5a',
  course49_v1: '#b46a22',
  course49_v2: '#7b4f89',
  course49_v3: '#9f3f2f',
  course49_v4: '#2f6f91',
  course49_v5: '#8b6b18',
  course49_v6: '#176b87',
  course49_v7: '#49744a',
  course49_v8: '#8a4f64',
  course49_v9: '#ad3c2f',
  pairs_arbitrage_v1: '#3f6f8f',
  weekly_triangle_v1: '#8b6b18',
  weekly_bull_platform_v1: '#2f6f91',
}

const attributionNames: Record<string, string> = {
  CAPITAL_AND_BOARD: '资金 + 封板确认',
  CAPITAL_ONLY: '仅资金确认',
  BOARD_ONLY: '仅封板确认',
  BASIC: '基础信号',
}

const styleNames: Record<string, string> = {
  SMALL_CAP_SPECULATION: '小盘投机', BROAD_RISK_ON: '全面风险偏好', MIXED: '混合风格',
  GROWTH_TREND: '成长趋势', LARGE_CAP_TREND: '大盘趋势', DEFENSIVE: '防御', UNKNOWN: '数据不足',
  MARKET_NEUTRAL: '市场中性',
}

const modeNames: Record<string, string> = {
  RECOVERY_IGNITION: '修复启动', FERMENT_SECOND_BOARD: '发酵二板',
  ACCELERATION_CORE_RELAY: '加速核心接力', HOLDING_MANAGEMENT: '持仓管理',
  PAIR_MEAN_REVERSION: '配对均值回归',
  SMALL_CAP_ACCELERATION_FIRST_BOARD: '小盘加速首板',
  BROAD_RISK_ON_FIRST_BOARD_RESEAL: '全面风险偏好首板回封',
  BROAD_RISK_ON_LOW_CROWDING_RESEAL: '全面风险偏好低拥挤回封',
  LEADER_PULLBACK_RECLAIM: '强势回调确认低吸',
}

const playbookNames: Record<string, string> = {
  recovery_ignition: '修复启动',
  ferment_second_board: '发酵二板',
  acceleration_core_relay: '加速核心接力',
  leader_pullback_reclaim: '强势回调低吸（研究）',
}

const exitNames: Record<string, string> = {
  FIXED_STOP: '固定止损', CAPITAL_DISTRIBUTION: '资金派发', MARKET_ICE: '市场冰点',
  MARKET_WEAK_CONFIRMED: '市场弱势确认', SECTOR_FADED_CONFIRMED: '题材退潮信号',
  LEADER_LOST_CONFIRMED: '龙头失位确认', TRAILING_PROFIT: '追踪止盈',
  FIRST_BOARD_TIME_EXIT: '首板五日到期',
  BROAD_FIRST_BOARD_TIME_EXIT: '首板回封三日到期',
  PULLBACK_TRAILING_PROFIT: '低吸追踪止盈',
  PULLBACK_STRUCTURE_BROKEN: '低吸结构破坏',
  PULLBACK_MARKET_WEAK_CONFIRMED: '低吸市场弱势确认',
  PULLBACK_SECTOR_FADED_CONFIRMED: '低吸题材退潮确认',
  PULLBACK_TIME_EXIT: '低吸五日到期',
}

export type FormState = {
  strategyId: BacktestRequest['strategy_id']
  startDate: string
  endDate: string
  dailyBars: string
  maxStocks: string
  samplingMode: SamplingMode
  sampleSeed: string
  executionCostMultiplier: string
  universe: BacktestUniverse
  stockCodes: string
  refreshData: boolean
  playbookIds: string[]
  pitReleaseId: string
}

function localDate(offsetDays = 0) {
  const value = new Date()
  value.setDate(value.getDate() + offsetDays)
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function parseStockCodes(value: string) {
  return value.split(/[\s,;，；]+/).map((item) => item.trim()).filter(Boolean)
}

export function requiresPitRelease(strategyId: string) {
  return strategyId === 'us_momentum_v1'
}

export function buildBacktestRequest(form: FormState): BacktestRequest {
  const strictUS = requiresPitRelease(form.strategyId)
  return {
    strategy_id: form.strategyId,
    start_date: form.startDate || undefined,
    end_date: form.endDate || undefined,
    daily_bars: Number(form.dailyBars),
    max_stocks: strictUS || !form.maxStocks ? undefined : Number(form.maxStocks),
    sampling_mode: strictUS ? 'full' : form.samplingMode,
    sample_seed: Number(form.sampleSeed) || 49,
    execution_cost_multiplier: Number(form.executionCostMultiplier) || 1,
    universe: strictUS ? 'sp500_ivv_proxy_v1' : form.universe,
    stock_codes: strictUS ? [] : parseStockCodes(form.stockCodes),
    refresh_data: strictUS ? false : form.refreshData,
    playbook_ids: form.strategyId === 'course49_system' ? form.playbookIds : [],
    pit_release_id: strictUS ? form.pitReleaseId || undefined : undefined,
  }
}

export function compressStates(states: BacktestState[]) {
  const rows: Array<BacktestState & { end_timestamp: string; days: number }> = []
  for (const item of [...states].sort((left, right) => left.timestamp.localeCompare(right.timestamp))) {
    const previous = rows.at(-1)
    const sameState = previous
      && previous.strategy_id === item.strategy_id
      && previous.market_phase === item.market_phase
      && previous.market_style === item.market_style
      && previous.trade_mode === item.trade_mode
      && previous.entry_allowed === item.entry_allowed
      && previous.suitability === item.suitability
    if (sameState) {
      previous.end_timestamp = item.timestamp
      previous.days += 1
    } else {
      rows.push({ ...item, end_timestamp: item.timestamp, days: 1 })
    }
  }
  return rows
}

export function backtestValidation(tradingDays: number, closedTrades: number) {
  const requiredDays = 250
  const requiredClosedTrades = 30
  return {
    validated: tradingDays >= requiredDays && closedTrades >= requiredClosedTrades,
    tradingDays,
    closedTrades,
    requiredDays,
    requiredClosedTrades,
  }
}

function money2(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return '-'
  return new Intl.NumberFormat('zh-CN', {
    style: 'currency',
    currency: 'CNY',
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  }).format(value)
}

function ratio(value?: number | null) {
  if (value == null || !Number.isFinite(value)) return '-'
  return new Intl.NumberFormat('zh-CN', { style: 'percent', minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value)
}

function decimal(value?: number | null, digits = 2) {
  if (value == null || !Number.isFinite(value)) return '-'
  return value.toFixed(digits)
}

function day(value?: string | null) {
  return value ? value.slice(0, 10) : '-'
}

export default function BacktestsPage() {
  const client = useQueryClient()
  const [selected, setSelected] = useState<string | null>(null)
  const [jobId, setJobId] = useState<string | null>(null)
  const [formError, setFormError] = useState('')
  const [showArchived, setShowArchived] = useState(false)
  const [form, setForm] = useState<FormState>({
    strategyId: 'course49_system',
    startDate: localDate(-365),
    endDate: localDate(),
    dailyBars: '320',
    maxStocks: '',
    samplingMode: 'full',
    sampleSeed: '49',
    executionCostMultiplier: '1',
    universe: 'all_a',
    stockCodes: '',
    refreshData: false,
    playbookIds: ['recovery_ignition', 'ferment_second_board', 'acceleration_core_relay'],
    pitReleaseId: '',
  })

  const list = useQuery({ queryKey: ['backtests'], queryFn: api.backtests, refetchInterval: 20_000 })
  const catalog = useQuery({ queryKey: ['strategy-catalog'], queryFn: api.strategyCatalog })
  const pitReleases = useQuery({ queryKey: ['us-pit-releases'], queryFn: api.usPitReleases })
  const readyPitReleases = useMemo(
    () => (pitReleases.data ?? []).filter((release) => release.status === 'DATA_READY'),
    [pitReleases.data],
  )
  const strategyLabels = useMemo(() => ({
    ...fallbackStrategyNames,
    ...Object.fromEntries((catalog.data?.strategies ?? []).map((item) => [
      item.strategy_id,
      strategyCatalogDisplayName(item.name, item.lifecycle),
    ])),
    ...Object.fromEntries((catalog.data?.archived_strategies ?? []).map((item) => [
      item.strategy_id,
      strategyCatalogDisplayName(item.name, item.lifecycle),
    ])),
    ...Object.fromEntries((catalog.data?.groups ?? []).map((item) => [item.group_id, item.name])),
  }), [catalog.data])
  const strategyMarkets = useMemo(() => Object.fromEntries(
    [
      ...[...(catalog.data?.strategies ?? []), ...(catalog.data?.archived_strategies ?? [])]
        .map((item) => [
          item.strategy_id,
          item.asset_classes.some((asset) => asset.startsWith('US_')) ? 'US' : 'CN',
        ] as const),
      ...(catalog.data?.groups ?? []).map((group) => {
        const memberMarkets = new Set(group.members.map((member) => {
          const strategy = [...(catalog.data?.strategies ?? []), ...(catalog.data?.archived_strategies ?? [])]
            .find((item) => item.strategy_id === member.strategy_id)
          return strategy?.asset_classes.some((asset) => asset.startsWith('US_')) ? 'US' : 'CN'
        }))
        return [group.group_id, memberMarkets.size === 1 && memberMarkets.has('US') ? 'US' : 'CN'] as const
      }),
    ],
  ), [catalog.data])
  const strategyOptions = useMemo(() => [
    ...(catalog.data?.groups ?? []).filter((item) => item.backtest_supported && (showArchived || item.category !== 'research_archive')).map((item) => ({
      id: item.group_id,
      name: item.name,
      type: item.category === 'framework' ? '体系' : item.category === 'research_archive' ? '研究归档' : '组合',
    })),
    ...(catalog.data?.strategies ?? []).filter((item) => item.backtest_enabled).map((item) => ({
      id: item.strategy_id,
      name: strategyCatalogDisplayName(item.name, item.lifecycle),
      type: item.framework_id ? '体系' : '独立策略',
    })),
    ...(showArchived ? (catalog.data?.archived_strategies ?? []).filter((item) => item.backtest_enabled).map((item) => ({
      id: item.strategy_id,
      name: strategyCatalogDisplayName(item.name, item.lifecycle),
      type: '研究归档',
    })) : []),
  ], [catalog.data, showArchived])
  const activeId = selected ?? list.data?.[0]?.backtest_id ?? null
  const detail = useQuery({
    queryKey: ['backtest', activeId],
    queryFn: () => api.backtest(activeId!),
    enabled: Boolean(activeId),
  })
  const job = useQuery({
    queryKey: ['job', jobId],
    queryFn: () => api.job(jobId!),
    enabled: Boolean(jobId),
    refetchInterval: (query) => ['QUEUED', 'RUNNING'].includes(query.state.data?.status ?? '') ? 1_500 : false,
  })
  const run = useMutation({
    mutationFn: api.runBacktest,
    onSuccess: (result) => {
      setJobId(result.job_id)
      client.invalidateQueries({ queryKey: ['jobs'] })
    },
  })
  const replay = useMutation({
    mutationFn: api.replayBacktest,
    onSuccess: (result) => {
      setJobId(result.job_id)
      client.invalidateQueries({ queryKey: ['jobs'] })
    },
  })

  useEffect(() => {
    if (job.data?.status === 'SUCCEEDED') {
      setSelected(null)
      client.invalidateQueries({ queryKey: ['backtests'] })
    }
  }, [client, job.data?.status])

  useEffect(() => {
    if (!requiresPitRelease(form.strategyId)) return
    if (readyPitReleases.some((release) => release.release_id === form.pitReleaseId)) return
    setForm((current) => ({ ...current, pitReleaseId: readyPitReleases[0]?.release_id ?? '' }))
  }, [form.pitReleaseId, form.strategyId, readyPitReleases])

  const chartData = useMemo(() => {
    const grouped = new Map<string, Record<string, string | number>>()
    for (const row of detail.data?.equity ?? []) {
      const key = row.timestamp.slice(0, 10)
      const item = grouped.get(key) ?? { date: key }
      item[row.strategy_id] = row.equity
      grouped.set(key, item)
    }
    return Array.from(grouped.values())
  }, [detail.data])

  const chartStrategies = useMemo(
    () => Array.from(new Set((detail.data?.equity ?? []).map((row) => row.strategy_id))),
    [detail.data],
  )
  const trades = useMemo(() => [...(detail.data?.trades ?? [])].reverse(), [detail.data])
  const positionChanges = useMemo(() => [...(detail.data?.position_changes ?? [])].reverse(), [detail.data])
  const metrics = detail.data?.metrics
  const course49Metrics = metrics?.components?.course49_system
    ?? metrics?.components?.course49_v11
    ?? metrics?.components?.course49_v10
    ?? metrics?.components?.course49_v9
    ?? metrics?.components?.course49_v8
    ?? metrics?.components?.course49_v7
    ?? metrics?.components?.course49_v6
    ?? metrics?.components?.course49_v5
    ?? metrics?.components?.course49_v4
    ?? metrics?.components?.course49_v3
    ?? metrics?.components?.course49_v2
    ?? metrics?.components?.course49_v1
    ?? metrics
  const pairMetrics = metrics?.components?.pairs_arbitrage_v1
    ?? Object.values(metrics?.components ?? {}).find((item) => item.execution_model === 'MULTI_LEG')
    ?? (detail.data?.strategy_id === 'pairs_arbitrage_v1' || metrics?.execution_model === 'MULTI_LEG' ? metrics : undefined)
  const attribution = course49Metrics?.course49_attribution ?? []
  const styleAttribution = course49Metrics?.style_attribution ?? []
  const modeAttribution = course49Metrics?.trade_mode_attribution ?? []
  const exitAttribution = course49Metrics?.exit_reason_attribution ?? []
  const playbookAttribution = course49Metrics?.playbook_attribution ?? detail.data?.playbook_attribution ?? []
  const executionFunnel = course49Metrics?.execution_funnel
  const closedTrades = attribution.length
    ? attribution.reduce((total, item) => total + (item.closed ?? 0), 0)
    : (course49Metrics?.closed_trades ?? Math.floor((course49Metrics?.trades ?? 0) / 2))
  const validation = backtestValidation(course49Metrics?.trading_days ?? metrics?.trading_days ?? 0, closedTrades)
  const backendValidation = course49Metrics?.validation ?? metrics?.validation
  const evidenceSufficient = backendValidation?.evidence_sufficient ?? validation.validated
  const targetMet = backendValidation?.target_met ?? ((metrics?.annualized_return ?? 0) >= 0.20)
  const targetVerified = backendValidation?.target_verified ?? false
  const states = useMemo(
    () => {
      const all = detail.data?.states ?? []
      for (const strategyId of ['course49_system', 'course49_v11', 'course49_v10', 'course49_v9', 'course49_v8', 'course49_v7', 'course49_v6', 'course49_v5', 'course49_v4', 'course49_v3', 'course49_v2']) {
        const adaptive = all.filter((item) => item.strategy_id === strategyId)
        if (adaptive.length) return adaptive
      }
      return all.filter((item) => item.strategy_id === 'pairs_arbitrage_v1')
    },
    [detail.data],
  )
  const timelineStates = useMemo(() => compressStates(states), [states])
  const distribution = detail.data?.parameters?.universe_distribution
  const activeJob = run.isPending || replay.isPending || ['QUEUED', 'RUNNING'].includes(job.data?.status ?? '')
  const canReplayFrozenSnapshot = Boolean(
    detail.data?.snapshot_id && (
      /^course49_v\d+$/.test(detail.data?.strategy_id ?? '')
      || detail.data?.strategy_id === 'course49_system'
      || detail.data?.strategy_id === 'chan_v1'
      || detail.data?.strategy_id === 'us_momentum_v1'
    ),
  )

  function update<K extends keyof FormState>(key: K, value: FormState[K]) {
    setForm((current) => ({ ...current, [key]: value }))
  }

  function updateStrategy(strategyId: FormState['strategyId']) {
    const strictUS = requiresPitRelease(strategyId)
    setForm((current) => ({
      ...current,
      strategyId,
      universe: strictUS ? 'sp500_ivv_proxy_v1' : strategyMarkets[strategyId] === 'US' ? 'all_us' : (
        current.universe === 'all_us' || current.universe === 'sp500_ivv_proxy_v1' ? 'all_a' : current.universe
      ),
      stockCodes: '',
      samplingMode: strategyMarkets[strategyId] === 'US' ? 'full' : current.samplingMode,
      maxStocks: strategyMarkets[strategyId] === 'US' ? '' : current.maxStocks,
      refreshData: strictUS ? false : current.refreshData,
      pitReleaseId: strictUS ? readyPitReleases[0]?.release_id ?? '' : current.pitReleaseId,
    }))
  }

  function submit(event: FormEvent) {
    event.preventDefault()
    if (form.startDate && form.endDate && form.startDate > form.endDate) {
      setFormError('开始日期不能晚于结束日期')
      return
    }
    const stockCodes = parseStockCodes(form.stockCodes)
    if (!requiresPitRelease(form.strategyId) && form.universe === 'custom' && stockCodes.length === 0) {
      setFormError('自定义股票池不能为空')
      return
    }
    if (requiresPitRelease(form.strategyId) && !form.pitReleaseId) {
      setFormError('美股动量严格回测必须选择一个 DATA_READY PIT release')
      return
    }
    setFormError('')
    run.mutate(buildBacktestRequest(form))
  }

  function replayWithDoubleCosts() {
    if (!detail.data || !canReplayFrozenSnapshot) return
    replay.mutate({
      source_backtest_id: detail.data.backtest_id,
      strategy_id: detail.data.strategy_id,
      execution_cost_multiplier: 2,
    })
  }

  return <>
    <PageHeader title="回测研究" />

    <section className="backtest-config panel">
      <div className="section-heading">
        <h2>回测参数</h2>
        {job.data && <StatusBadge status={job.data.status} />}
      </div>
      <form className="config-grid" onSubmit={submit}>
        <label className="field"><span>策略</span><select value={form.strategyId} onChange={(event) => updateStrategy(event.target.value as FormState['strategyId'])}>
          {strategyOptions.length ? strategyOptions.map((item) => <option key={item.id} value={item.id}>{item.type} · {item.name}</option>) : <option value="course49_system">49课体系</option>}
        </select></label>
        <label className="field toggle-field"><input type="checkbox" checked={showArchived} onChange={(event) => setShowArchived(event.target.checked)} /><span>显示研究归档</span></label>
        <label className="field"><span>开始日期</span><input type="date" value={form.startDate} max={form.endDate || undefined} onChange={(event) => update('startDate', event.target.value)} /></label>
        <label className="field"><span>结束日期</span><input type="date" value={form.endDate} min={form.startDate || undefined} onChange={(event) => update('endDate', event.target.value)} /></label>
        <label className="field"><span>股票池</span><select value={form.universe} onChange={(event) => update('universe', event.target.value as BacktestUniverse)}>
          {Object.entries(universeNames).filter(([value]) => (
            requiresPitRelease(form.strategyId)
              ? value === 'sp500_ivv_proxy_v1'
              : strategyMarkets[form.strategyId] === 'US'
              ? value === 'all_us' || value === 'custom'
              : value !== 'all_us' && value !== 'sp500_ivv_proxy_v1'
          )).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        {requiresPitRelease(form.strategyId) && <label className="field field--wide"><span>PIT Release（只允许 DATA_READY）</span><select aria-label="PIT release" value={form.pitReleaseId} onChange={(event) => update('pitReleaseId', event.target.value)} disabled={pitReleases.isLoading || !readyPitReleases.length}>
          {readyPitReleases.length ? readyPitReleases.map((release) => <option key={release.release_id} value={release.release_id}>{release.release_id.slice(0, 12)} · {release.certified_start?.slice(0, 10) ?? '-'} 至 {release.certified_end?.slice(0, 10) ?? '-'}</option>) : <option value="">没有可用的 DATA_READY release</option>}
        </select></label>}
        <label className="field"><span>日线数量</span><input type="number" min="90" max="2000" value={form.dailyBars} onChange={(event) => update('dailyBars', event.target.value)} /></label>
        {strategyMarkets[form.strategyId] !== 'US' && <label className="field"><span>运行范围</span><select value={form.samplingMode} onChange={(event) => update('samplingMode', event.target.value as SamplingMode)}>
          <option value="full">全量</option><option value="stratified">分层样本</option>
        </select></label>}
        {strategyMarkets[form.strategyId] !== 'US' && form.samplingMode === 'stratified' && <label className="field"><span>股票上限</span><input type="number" min="1" value={form.maxStocks} onChange={(event) => update('maxStocks', event.target.value)} /></label>}
        {strategyMarkets[form.strategyId] !== 'US' && form.samplingMode === 'stratified' && <label className="field"><span>抽样种子</span><input type="number" value={form.sampleSeed} onChange={(event) => update('sampleSeed', event.target.value)} /></label>}
        <label className="field"><span>执行成本</span><select value={form.executionCostMultiplier} onChange={(event) => update('executionCostMultiplier', event.target.value)}><option value="1">标准成本</option><option value="2">2 倍压力</option></select></label>
        {!requiresPitRelease(form.strategyId) && <label className="field field--refresh toggle-field"><input type="checkbox" checked={form.refreshData} onChange={(event) => update('refreshData', event.target.checked)} /><span>重新读取通达信数据</span></label>}
        {form.strategyId === 'course49_system' && <fieldset className="playbook-selector">
          <legend>研究回测剧本</legend>
          {[
            ['recovery_ignition', '修复启动'],
            ['ferment_second_board', '发酵二板'],
            ['acceleration_core_relay', '加速核心接力'],
            ['leader_pullback_reclaim', '强势回调低吸（研究）'],
          ].map(([id, label]) => <label key={id}><input type="checkbox" checked={form.playbookIds.includes(id)} onChange={(event) => update('playbookIds', event.target.checked ? [...form.playbookIds, id] : form.playbookIds.filter((item) => item !== id))} />{label}</label>)}
        </fieldset>}
        {form.universe === 'custom' && <label className="field field--wide"><span>股票代码</span><textarea rows={2} value={form.stockCodes} onChange={(event) => update('stockCodes', event.target.value)} /></label>}
        {requiresPitRelease(form.strategyId) && <div className="field field--wide"><span>严格执行约束</span><strong>固定 IVV PIT 全量成员；禁止自定义股票、抽样、股票上限和运行时刷新</strong></div>}
        <div className="field field--action"><button className="button" type="submit" disabled={activeJob}><FlaskConical size={17} />{activeJob ? '回测运行中' : '运行回测'}</button></div>
      </form>
      {job.data && ['QUEUED', 'RUNNING'].includes(job.data.status) && <div className="job-progress" aria-live="polite">
        <div><strong>{job.data.detail || '准备运行'}</strong><span>{Math.round((job.data.progress ?? 0) * 100)}%</span></div>
        <progress max="1" value={job.data.progress ?? 0} />
        <small>{job.data.waiting_reason === 'single_tdx_channel' ? '通达信读取通道繁忙，任务会自动继续' : job.data.cache_status ? `数据状态：${job.data.cache_status}` : job.data.phase}</small>
      </div>}
      {(formError || run.error || replay.error || job.data?.status === 'FAILED') && <div className="form-error">{formError || run.error?.message || replay.error?.message || job.data?.error}</div>}
    </section>

    {metrics && <section className="metrics-band metrics-band--six">
      <div><span>总收益</span><strong className={(metrics.total_return ?? 0) >= 0 ? 'positive-text' : 'negative-text'}>{ratio(metrics.total_return)}</strong></div>
      <div><span>年化收益{targetVerified ? '（已验证）' : '（未验证）'}</span><strong>{ratio(metrics.annualized_return)}</strong></div>
      <div><span>最大回撤</span><strong className="negative-text">{ratio(metrics.max_drawdown)}</strong></div>
      <div><span>胜率</span><strong>{ratio(metrics.win_rate)}</strong></div>
      <div><span>期末权益</span><strong>{money2(metrics.final_equity)}</strong></div>
      <div><span>夏普 / 成交</span><strong>{decimal(metrics.sharpe_ratio)} <small>/ {metrics.trades ?? 0}</small></strong></div>
    </section>}

    {metrics && !evidenceSufficient && <section className="validation-notice" aria-label="回测验证状态">
      <strong>样本未达验证门槛</strong>
      <span>当前 {validation.tradingDays} 个交易日 · {validation.closedTrades} 笔平仓；年化收益仅为短期换算。门槛为 {validation.requiredDays} 个交易日和 {validation.requiredClosedTrades} 笔平仓。</span>
    </section>}

    {executionFunnel && <section className="table-section">
      <div className="section-heading"><div><span>EXECUTION</span><h3>买入执行漏斗</h3></div></div>
      <div className="table-wrap"><table className="comparison-table"><thead><tr><th>范围</th><th className="numeric">信号</th><th className="numeric">次日尝试</th><th className="numeric">成交</th><th className="numeric">成交率</th><th className="numeric">涨停阻塞</th><th className="numeric">缺口阻塞</th><th className="numeric">仓位限制</th><th className="numeric">资金不足</th></tr></thead><tbody>
        {[
          ['全部', executionFunnel] as const,
          ...Object.entries(executionFunnel.by_playbook ?? {}).map(([id, item]) => [playbookNames[id] ?? id, item] as const),
        ].map(([label, item]) => <tr key={label}><td>{label}</td><td className="numeric">{item.generated_buy_signals}</td><td className="numeric">{item.attempted_next_open}</td><td className="numeric">{item.filled_buy_orders}</td><td className="numeric">{ratio(item.fill_rate)}</td><td className="numeric">{item.blocked_limit_up_open}</td><td className="numeric">{item.blocked_open_gap}</td><td className="numeric">{item.blocked_portfolio}</td><td className="numeric">{item.blocked_insufficient_cash}</td></tr>)}
      </tbody></table></div>
    </section>}

    {metrics && evidenceSufficient && !targetMet && <section className="validation-notice" aria-label="收益目标状态">
      <strong>样本已足够，但未达到目标</strong>
      <span>当前年化收益 {ratio(metrics.annualized_return)}，低于 20% 目标；结果保留为有效反证，不标记为策略成功。</span>
    </section>}

    {metrics && evidenceSufficient && targetMet && !targetVerified && <section className="validation-notice" aria-label="样本外验证状态">
      <strong>历史收益目标已达到，稳健性仍未验证</strong>
      <span>年化收益达到 20% 只说明这一段历史回放达标；仍需通过非重叠历史样本、收益集中度、双倍成本和冻结日后的样本外检验。</span>
    </section>}

    {detail.data?.parameters && <section className="pool-summary panel">
      <div className="section-heading"><h2>实际股票池</h2><div><span>{detail.data.parameters.sampling_mode === 'stratified' ? `分层样本 · 种子 ${detail.data.parameters.sample_seed}` : '全量运行'}</span>{canReplayFrozenSnapshot && <button className="button button--secondary" type="button" disabled={activeJob} onClick={replayWithDoubleCosts}><FlaskConical size={16} />2 倍成本回放</button>}</div></div>
      <div className="pool-facts">
        <div><span>实际数量</span><strong>{distribution?.total || detail.data.parameters.resolved_symbols || detail.data.parameters.loaded_symbols || '-'}</strong></div>
        <div><span>{pairMetrics ? '平均总敞口' : '平均资金投入'}</span><strong>{ratio(pairMetrics?.average_gross_exposure ?? course49Metrics?.average_capital_invested)}</strong></div>
        <div><span>{pairMetrics ? '平均净敞口' : '板块成分口径'}</span><strong>{pairMetrics ? ratio(pairMetrics.average_net_exposure) : detail.data.parameters.sector_membership_quality ?? '-'}</strong></div>
        <div><span>股票池哈希</span><strong className="hash-value">{detail.data.parameters.stock_pool_hash?.slice(0, 12) ?? '-'}</strong></div>
        <div><span>数据来源</span><strong>{detail.data.parameters.cache_status === 'memory_hit' ? '内存复用' : detail.data.parameters.cache_status?.includes('hit') ? '快照复用' : '新读取'}</strong></div>
        <div><span>数据时点</span><strong>{detail.data.parameters.data_asof ?? '-'}</strong></div>
        {detail.data.parameters.pit_release_id && <div><span>PIT Release</span><strong className="hash-value" title={detail.data.parameters.pit_release_id}>{detail.data.parameters.pit_release_id.slice(0, 12)}</strong></div>}
        <div><span>本地线程</span><strong>{detail.data.parameters.worker_threads ?? '-'}</strong></div>
        <div><span>峰值内存</span><strong>{detail.data.parameters.peak_memory_bytes ? `${(detail.data.parameters.peak_memory_bytes / 1024 ** 3).toFixed(1)} GB` : '-'}</strong></div>
      </div>
      {detail.data.parameters.stage_durations_seconds && <div className="distribution-row">
        {Object.entries(detail.data.parameters.stage_durations_seconds).map(([name, seconds]) => <span key={name}>{name} <strong>{seconds.toFixed(1)}s</strong></span>)}
      </div>}
      {distribution && <div className="distribution-row">
        {Object.entries(distribution.segments).map(([name, count]) => <span key={name}>{name} <strong>{count}</strong></span>)}
      </div>}
    </section>}

    {pairMetrics && <section className="metrics-band metrics-band--compact">
      <div><span>交易组合</span><strong>{pairMetrics.pair_groups ?? 0}</strong></div>
      <div><span>完成组合</span><strong>{pairMetrics.completed_pair_groups ?? 0}</strong></div>
      <div><span>组合胜率</span><strong>{ratio(pairMetrics.pair_win_rate)}</strong></div>
      <div><span>组合盈亏</span><strong className={(pairMetrics.pair_total_pnl ?? 0) >= 0 ? 'positive-text' : 'negative-text'}>{money2(pairMetrics.pair_total_pnl)}</strong></div>
    </section>}

    <div className="backtest-layout">
      <section className="run-list panel">
        <div className="section-heading"><h2>实验记录</h2><span>{list.data?.length ?? 0}</span></div>
        {list.isLoading ? <LoadingState /> : list.error ? <ErrorState error={list.error} /> : !list.data?.length ? <EmptyState /> : list.data.map((item) => {
          const universe = item.parameters?.universe as BacktestUniverse | undefined
          return <button key={item.backtest_id} className={`run-item ${activeId === item.backtest_id ? 'active' : ''}`} onClick={() => setSelected(item.backtest_id)}>
            <div><strong>{strategyLabels[item.strategy_id] ?? item.strategy_id}</strong><span>{day(item.start_date)} 至 {day(item.end_date)}</span><span>{universe ? universeNames[universe] : time(item.started_at)} · {backtestCostLabel(item.parameters?.execution_cost_multiplier as number | undefined)}</span></div>
            <StatusBadge status={item.status} />
          </button>
        })}
      </section>
      <section className="chart-panel">
        <div className="section-heading"><h2>资金曲线</h2><span>{day(detail.data?.start_date)} 至 {day(detail.data?.end_date)}</span></div>
        {detail.isLoading ? <LoadingState /> : detail.error ? <ErrorState error={detail.error} /> : detail.data?.error ? <div className="error-line">{detail.data.error}</div> : chartData.length ? <div className="chart-wrap"><ResponsiveContainer width="100%" height="100%"><LineChart data={chartData} margin={{ top: 12, right: 16, bottom: 4, left: 0 }}><CartesianGrid stroke="#e2e4de" vertical={false} /><XAxis dataKey="date" tick={{ fontSize: 11 }} minTickGap={28} /><YAxis tick={{ fontSize: 11 }} width={64} /><Tooltip formatter={(value) => money2(Number(value))} /><Legend />{chartStrategies.map((strategyId, index) => <Line key={strategyId} type="monotone" dataKey={strategyId} name={strategyLabels[strategyId] ?? strategyId} stroke={lineColors[strategyId] ?? ['#555f78', '#8a5268'][index % 2]} dot={false} strokeWidth={2} />)}</LineChart></ResponsiveContainer></div> : <EmptyState />}
      </section>
    </div>

    {detail.data?.comparison?.length ? <section className="table-section">
      <div className="section-heading"><h2>策略对比</h2><span>{detail.data.comparison.length} 个子策略</span></div>
      <div className="table-wrap"><table className="comparison-table"><thead><tr><th>策略</th><th className="numeric">初始资金</th><th className="numeric">期末权益</th><th className="numeric">总收益</th><th className="numeric">年化收益</th><th className="numeric">最大回撤</th><th className="numeric">夏普</th><th className="numeric">胜率</th><th className="numeric">成交</th></tr></thead><tbody>
        {detail.data.comparison.map((item) => <tr key={item.strategy_id}><td>{strategyLabels[item.strategy_id] ?? item.strategy_id}</td><td className="numeric">{money2(item.initial_cash)}</td><td className="numeric">{money2(item.final_equity)}</td><td className={`numeric ${(item.total_return ?? 0) >= 0 ? 'positive-text' : 'negative-text'}`}>{ratio(item.total_return)}</td><td className="numeric">{ratio(item.annualized_return)}</td><td className="numeric negative-text">{ratio(item.max_drawdown)}</td><td className="numeric">{decimal(item.sharpe_ratio)}</td><td className="numeric">{ratio(item.win_rate)}</td><td className="numeric">{item.trades ?? 0}</td></tr>)}
      </tbody></table></div>
    </section> : null}

    {states.length ? <section className="table-section">
      <div className="section-heading"><h2>市场风格时间线</h2><span>{states.length} 个交易日 · {timelineStates.length} 个区间</span></div>
      <div className="timeline-strip">
        {timelineStates.map((item) => <div key={`${item.strategy_id}-${item.timestamp}`} className={`timeline-state ${item.entry_allowed ? 'timeline-state--active' : ''}`}>
          <span>{item.days > 1 ? `${day(item.timestamp)} - ${day(item.end_timestamp)} · ${item.days}日` : day(item.timestamp)}</span>
          <strong>{styleNames[item.market_style] ?? item.market_style}</strong>
          <small>{item.market_phase} · {ratio(item.suitability)}</small>
          <small>{item.trade_mode ? item.trade_mode.split(',').map((mode) => modeNames[mode] ?? mode).join(' / ') : '观望'}</small>
        </div>)}
      </div>
    </section> : null}

    {(styleAttribution.length || modeAttribution.length || playbookAttribution.length || exitAttribution.length) ? <section className="table-section">
      <div className="section-heading"><h2>49课策略归因</h2><span>风格、交易模式与退出原因</span></div>
      <div className="attribution-grid">
        {styleAttribution.length ? <AttributionTable title="市场风格" rows={styleAttribution} field="market_style" names={styleNames} /> : null}
        {modeAttribution.length ? <AttributionTable title="交易模式" rows={modeAttribution} field="trade_mode" names={modeNames} /> : null}
        {playbookAttribution.length ? <AttributionTable title="来源剧本" rows={playbookAttribution} field="playbook_id" names={playbookNames} /> : null}
        {exitAttribution.length ? <div className="attribution-block"><h3>退出原因</h3><div className="table-wrap"><table><thead><tr><th>原因</th><th className="numeric">次数</th><th className="numeric">盈利</th><th className="numeric">累计盈亏</th></tr></thead><tbody>{exitAttribution.map((item) => <tr key={item.reason}><td>{exitNames[item.reason] ?? item.reason}</td><td className="numeric">{item.count}</td><td className="numeric">{item.wins}</td><td className={`numeric ${item.total_pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money2(item.total_pnl)}</td></tr>)}</tbody></table></div></div> : null}
      </div>
    </section> : null}

    {attribution.some((item) => item.entries > 0) ? <section className="table-section">
      <div className="section-heading"><h2>49课确认因子归因</h2><span>按入场证据分组</span></div>
      <div className="table-wrap"><table className="attribution-table"><thead><tr><th>确认组合</th><th className="numeric">入场</th><th className="numeric">已平仓</th><th className="numeric">盈利笔数</th><th className="numeric">胜率</th><th className="numeric">累计盈亏</th><th className="numeric">单笔平均</th></tr></thead><tbody>
        {attribution.map((item) => <tr key={item.cohort}><td>{attributionNames[item.cohort] ?? item.cohort}</td><td className="numeric">{item.entries}</td><td className="numeric">{item.closed}</td><td className="numeric">{item.wins}</td><td className="numeric">{ratio(item.win_rate)}</td><td className={`numeric ${item.total_pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money2(item.total_pnl)}</td><td className={`numeric ${item.avg_pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money2(item.avg_pnl)}</td></tr>)}
      </tbody></table></div>
    </section> : null}

    <section className="table-section">
      <div className="section-heading"><h2>逐笔交易</h2><span>{trades.length} 笔</span></div>
      {trades.length ? <div className="table-wrap"><table className="trade-table"><thead><tr><th>时间</th><th>策略</th><th>代码</th><th>方向</th><th className="numeric">数量</th><th className="numeric">成交价</th><th className="numeric">费用</th><th className="numeric">盈亏</th><th>原因</th></tr></thead><tbody>
        {trades.map((trade, index) => <tr key={`${trade.strategy_id}-${trade.timestamp}-${trade.code}-${index}`}><td>{time(trade.timestamp)}</td><td>{strategyLabels[trade.strategy_id] ?? trade.strategy_id}</td><td className="symbol">{trade.code}{trade.group_key && <small className="subline">{trade.group_key}</small>}</td><td className={trade.side === 'BUY' || trade.side === 'COVER' ? 'positive-text' : 'negative-text'}>{trade.side}</td><td className="numeric">{trade.quantity}</td><td className="numeric">{trade.price.toFixed(2)}</td><td className="numeric">{money2(trade.fees)}</td><td className={`numeric ${(trade.pnl ?? 0) >= 0 ? 'positive-text' : 'negative-text'}`}>{trade.pnl == null ? '-' : money2(trade.pnl)}</td><td><div className="reason-list"><span>{trade.reason || '-'}</span></div></td></tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>持仓变化</h2><span>{positionChanges.length} 次</span></div>
      {positionChanges.length ? <div className="table-wrap"><table className="position-change-table"><thead><tr><th>时间</th><th>策略</th><th>代码</th><th className="numeric">变化</th><th className="numeric">变化前</th><th className="numeric">变化后</th><th className="numeric">价格</th><th className="numeric">成交金额</th></tr></thead><tbody>
        {positionChanges.map((change, index) => <tr key={`${change.strategy_id}-${change.timestamp}-${change.code}-${index}`}><td>{time(change.timestamp)}</td><td>{strategyLabels[change.strategy_id] ?? change.strategy_id}</td><td className="symbol">{change.code}</td><td className={`numeric ${change.quantity_change >= 0 ? 'positive-text' : 'negative-text'}`}>{change.quantity_change > 0 ? '+' : ''}{change.quantity_change}</td><td className="numeric">{change.quantity_before}</td><td className="numeric">{change.quantity_after}</td><td className="numeric">{change.price.toFixed(2)}</td><td className="numeric">{money2(change.trade_value)}</td></tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>

    {job.data?.status === 'SUCCEEDED' && <div className="toast">回测完成，实验记录已更新</div>}
  </>
}

function AttributionTable({ title, rows, field, names }: {
  title: string
  rows: AttributionRow[]
  field: 'market_style' | 'trade_mode' | 'playbook_id'
  names: Record<string, string>
}) {
  return <div className="attribution-block"><h3>{title}</h3><div className="table-wrap"><table><thead><tr><th>{title}</th><th className="numeric">入场</th><th className="numeric">平仓</th><th className="numeric">胜率</th><th className="numeric">累计盈亏</th></tr></thead><tbody>{rows.map((item) => {
    const key = String(item[field] ?? 'UNKNOWN')
    return <tr key={key}><td>{names[key] ?? key}</td><td className="numeric">{item.entries}</td><td className="numeric">{item.closed}</td><td className="numeric">{ratio(item.win_rate)}</td><td className={`numeric ${item.total_pnl >= 0 ? 'positive-text' : 'negative-text'}`}>{money2(item.total_pnl)}</td></tr>
  })}</tbody></table></div></div>
}
