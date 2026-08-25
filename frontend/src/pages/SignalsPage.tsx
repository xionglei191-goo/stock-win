import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, X } from 'lucide-react'
import { useState } from 'react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent, time } from '../components'
import type { DecisionRequest, Signal } from '../types'

type Evidence = Record<string, unknown>

const phaseNames: Record<string, string> = {
  ICE: '冰点', RECOVERY: '修复', FERMENT: '发酵', ACCELERATION: '加速',
  CLIMAX: '高潮', DIVERGENCE: '分歧', RETREAT: '退潮', NORMAL: '常态',
  START: '启动',
}

const roleNames: Record<string, string> = {
  SPACE_LEADER: '空间龙头', THEME_LEADER: '题材龙头',
  CAPACITY_CORE: '容量核心', CHALLENGER: '补涨候选',
}

const styleNames: Record<string, string> = {
  SMALL_CAP_SPECULATION: '小盘投机', BROAD_RISK_ON: '全面风险偏好', MIXED: '混合风格',
  GROWTH_TREND: '成长趋势', LARGE_CAP_TREND: '大盘趋势', DEFENSIVE: '防御', UNKNOWN: '数据不足',
}

const modeNames: Record<string, string> = {
  RECOVERY_IGNITION: '修复启动', FERMENT_SECOND_BOARD: '发酵二板',
  ACCELERATION_CORE_RELAY: '加速核心接力', HOLDING_MANAGEMENT: '持仓管理',
  LOCAL_ACCELERATION_CORE: '局部加速核心', LOCAL_ACCELERATION_HIGH_BOARD: '局部加速高标',
  SMALL_CAP_ACCELERATION_FIRST_BOARD: '小盘加速首板',
  BROAD_RISK_ON_FIRST_BOARD_RESEAL: '全面风险偏好首板回封',
  BROAD_RISK_ON_LOW_CROWDING_RESEAL: '全面风险偏好低拥挤回封',
}

const playbookNames: Record<string, string> = {
  recovery_ignition: '修复启动',
  ferment_second_board: '发酵二板',
  acceleration_core_relay: '加速核心接力',
  holding_management: '持仓管理',
}

const reasonNames: Record<string, string> = {
  SECOND_BOARD_CAPITAL_CONFIRMED: '二板资金确认', SECOND_BOARD_LEADER: '二板龙头',
  SECOND_BOARD_QUALITY_CONFIRMED: '二板封板确认', EARLY_SEAL: '早盘封板', SEALED_ONCE: '一次封板',
  STRONG_SEAL: '强封单', AUCTION_STRENGTH: '竞价强势', RELIABLE_FIRST_BOARD: '首板可靠',
  PREMIUM_MEMORY: '历史溢价',
  HIGH_BOARD_RELAY: '高标接力', TOP_THEME: '主流题材', SPACE_LEADER: '空间龙头',
  THEME_LEADER: '题材龙头', CAPACITY_CORE: '容量核心', CHALLENGER: '补涨候选',
  LHB_NET_BUY: '龙虎榜净买', INSTITUTION_BUY: '机构净买', NORTHBOUND_BUY: '北向净买',
  REPEATED_LIST: '连续上榜', CAPITAL_DISTRIBUTION: '资金派发', MARKET_RETREAT: '市场退潮',
  SECTOR_FADED: '题材转弱', LEADER_LOST: '龙头地位丢失', BELOW_MA5: '跌破五日线',
  FIXED_STOP: '固定止损',
  RECOVERY_IGNITION: '修复启动', FERMENT_SECOND_BOARD: '发酵二板',
  ACCELERATION_CORE_RELAY: '加速核心接力', TOP3_THEME: '前三题材',
  MARKET_ICE: '市场冰点', MARKET_WEAK_CONFIRMED: '市场弱势确认',
  SECTOR_FADED_CONFIRMED: '题材退潮确认', LEADER_LOST_CONFIRMED: '龙头失位确认',
  TRAILING_PROFIT: '追踪止盈',
  LOCAL_ACCELERATION_CORE: '局部加速核心', LOCAL_ACCELERATION_HIGH_BOARD: '局部加速高标',
  SMALL_CAP_ACCELERATION_FIRST_BOARD: '小盘加速首板',
  FIRST_BOARD_TIME_EXIT: '首板五日到期',
  BROAD_FIRST_BOARD_TIME_EXIT: '首板回封三日到期',
}

const decisionReasons = [
  '策略证据充分', '风险可接受', '市场状态匹配', '证据不足',
  '风险过高', '与持仓冲突', '主观观察',
]

const strategyNames: Record<string, string> = {
  course49_system: '49课体系',
  course49_v10: '49课 V10（留出目标否决）',
  course49_v11: '49课 V11（历史稳健性否决）',
  chan_v1: '缠论',
  course49_v1: '49课 V1',
  course49_v2: '49课 V2',
  course49_v3: '49课 V3',
  course49_v4: '49课 V4',
  course49_v5: '49课 V5',
  course49_v6: '49课 V6',
  course49_v7: '49课 V7',
  course49_v8: '49课 V8',
  course49_v9: '49课 V9（留出否决）',
  weekly_triangle_v1: '周线收敛三角形',
  weekly_bull_platform_v1: '周线底部平台多头',
}

export function DecisionDialog({ signal, decision, pending, onClose, onConfirm }: {
  signal: Signal
  decision: 'APPROVED' | 'REJECTED'
  pending: boolean
  onClose: () => void
  onConfirm: (payload: DecisionRequest) => void
}) {
  const [tags, setTags] = useState<string[]>([])
  const [confidence, setConfidence] = useState(60)
  const [maxLoss, setMaxLoss] = useState('')
  const [note, setNote] = useState('')
  const [pushTdx, setPushTdx] = useState(false)
  const [validation, setValidation] = useState('')
  const review = signal.ai_review
  const submit = () => {
    if (!tags.length) {
      setValidation('请至少选择一个决策理由')
      return
    }
    const parsedLoss = maxLoss === '' ? null : Number(maxLoss)
    if (parsedLoss !== null && (!Number.isFinite(parsedLoss) || parsedLoss < 0 || parsedLoss > 100)) {
      setValidation('最大可接受亏损需在 0–100% 之间')
      return
    }
    onConfirm({
      decision,
      note: note.trim(),
      push_tdx: decision === 'APPROVED' && pushTdx,
      reason_tags: tags,
      confidence,
      max_acceptable_loss: parsedLoss === null ? null : parsedLoss / 100,
      ai_review_id: review?.review_id,
    })
  }
  return <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
    <section className="decision-dialog" role="dialog" aria-modal="true" aria-labelledby="decision-title" onMouseDown={(event) => event.stopPropagation()}>
      <div className="dialog-header">
        <div><h2 id="decision-title">{decision === 'APPROVED' ? '批准候选' : '拒绝候选'}</h2><span>{signal.code} · {signal.strategy_id}</span></div>
        <button className="icon-button" title="关闭" onClick={onClose}><X size={17} /></button>
      </div>
      {review && <div className="ai-review-strip">
        <div><span>AI 意见</span><strong>{review.recommendation}</strong><small>置信度 {percent(review.confidence)}</small></div>
        <p>{review.summary}</p>
      </div>}
      <fieldset className="reason-options"><legend>决策理由</legend>
        {decisionReasons.map((tag) => <label key={tag}><input type="checkbox" checked={tags.includes(tag)} onChange={() => setTags((current) => current.includes(tag) ? current.filter((item) => item !== tag) : [...current, tag])} /><span>{tag}</span></label>)}
      </fieldset>
      <label className="field"><span>决策置信度 <strong>{confidence}</strong></span><input type="range" min="0" max="100" value={confidence} onChange={(event) => setConfidence(Number(event.target.value))} /></label>
      <div className="dialog-grid">
        <label className="field"><span>最大可接受亏损（%）</span><input type="number" min="0" max="100" step="0.1" value={maxLoss} onChange={(event) => setMaxLoss(event.target.value)} placeholder="可选" /></label>
        {decision === 'APPROVED' && <label className="toggle-field"><input type="checkbox" checked={pushTdx} onChange={(event) => setPushTdx(event.target.checked)} /><span>同步到通达信</span></label>}
      </div>
      <label className="field"><span>备注</span><textarea rows={3} maxLength={500} value={note} onChange={(event) => setNote(event.target.value)} /></label>
      {validation && <div className="form-error">{validation}</div>}
      <div className="dialog-actions"><button className="button button--secondary" onClick={onClose}>取消</button><button className="button" disabled={pending} onClick={submit}><Check size={16} />确认{decision === 'APPROVED' ? '批准' : '拒绝'}</button></div>
    </section>
  </div>
}

function record(value: unknown): Evidence {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Evidence : {}
}

function numeric(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function phase(value: unknown): string {
  const code = String(value ?? '')
  return phaseNames[code] ?? (code || '-')
}

export function marketSummary(evidence: Evidence): { phase: string; score: string; style: string; mode: string } {
  const score = numeric(evidence.market_score)
  const suitability = numeric(evidence.style_suitability)
  const styleCode = String(evidence.market_style ?? '')
  const modeCode = String(evidence.trade_mode ?? '')
  return {
    phase: phase(evidence.market_phase),
    score: score === null ? '-' : percent(score),
    style: styleCode ? `${styleNames[styleCode] ?? styleCode} ${suitability === null ? '' : percent(suitability)}`.trim() : '-',
    mode: modeNames[modeCode] ?? (modeCode || '-'),
  }
}

export function themeSummary(evidence: Evidence): { sector: string; phase: string; role: string; streak: string } {
  const streak = numeric(evidence.limit_streak)
  return {
    sector: String(evidence.sector_name ?? evidence.sector_code ?? '-'),
    phase: phase(evidence.theme_phase),
    role: roleNames[String(evidence.role ?? '')] ?? String(evidence.role ?? '-'),
    streak: streak === null ? '-' : `${streak}板`,
  }
}

export function capitalSummary(evidence: Evidence): { state: string; net: string; institution: string; risk: boolean } {
  const lhb = record(evidence.lhb)
  if (!lhb.listed) return { state: '未上榜', net: '-', institution: '-', risk: false }
  const net = numeric(lhb.net_buy_ratio)
  const institution = numeric(lhb.institution_net_ratio)
  const risk = Boolean(lhb.risk)
  return {
    state: risk ? '资金风险' : `上榜 ${String(lhb.event_date ?? '')}`,
    net: net === null ? '-' : percent(net),
    institution: institution === null ? '-' : percent(institution),
    risk,
  }
}

export function boardSummary(evidence: Evidence): { state: string; detail: string; risk: boolean } {
  const behavior = record(evidence.limit_behavior)
  if (!behavior.limit_event) return { state: '无涨停行为', detail: '-', risk: false }
  const score = numeric(behavior.board_quality_score)
  const openCount = numeric(behavior.open_board_count)
  const firstTime = String(behavior.first_limit_time ?? '')
  const displayTime = firstTime.length === 6 ? `${firstTime.slice(0, 2)}:${firstTime.slice(2, 4)}` : '-'
  const risk = Boolean(behavior.board_risk)
  return {
    state: risk ? String(behavior.board_risk) : `质量 ${score === null ? '-' : percent(score)}`,
    detail: `首封 ${displayTime} · 开板 ${openCount ?? '-'} 次`,
    risk,
  }
}

function SignalContext({ signal }: { signal: Signal }) {
  const market = marketSummary(signal.evidence)
  const theme = themeSummary(signal.evidence)
  const capital = capitalSummary(signal.evidence)
  const board = boardSummary(signal.evidence)
  return <>
    <td><div className="signal-context"><strong>{market.phase} · {market.style}</strong><span>评分 {market.score} · {market.mode}</span></div></td>
    <td><div className="signal-context"><strong>{theme.sector}</strong><span>{theme.phase} · {theme.role} · {theme.streak}</span></div></td>
    <td><div className={`signal-context ${capital.risk ? 'signal-context--risk' : ''}`}><strong>{capital.state}</strong><span>净买 {capital.net} · 机构 {capital.institution}</span></div></td>
    <td><div className={`signal-context ${board.risk ? 'signal-context--risk' : ''}`}><strong>{board.state}</strong><span>{board.detail}</span></div></td>
  </>
}

export default function SignalsPage() {
  const [filter, setFilter] = useState('PROPOSED')
  const [selected, setSelected] = useState<{ signal: Signal; decision: 'APPROVED' | 'REJECTED' } | null>(null)
  const queryClient = useQueryClient()
  const signals = useQuery({ queryKey: ['signals', filter], queryFn: () => api.signals(filter || undefined) })
  const decision = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: DecisionRequest }) => api.decide(id, payload),
    onSuccess: () => {
      setSelected(null)
      queryClient.invalidateQueries({ queryKey: ['signals'] })
      queryClient.invalidateQueries({ queryKey: ['dashboard'] })
    },
  })
  return <>
    <PageHeader title="候选与信号" actions={<div className="segmented">
      {['PROPOSED', 'APPROVED', 'EXECUTED', ''].map((item) => <button key={item || 'ALL'} className={filter === item ? 'active' : ''} onClick={() => setFilter(item)}>{item || '全部'}</button>)}
    </div>} />
    {signals.isLoading ? <LoadingState /> : signals.error ? <ErrorState error={signals.error} /> : !signals.data?.length ? <EmptyState /> : <section className="table-section table-section--flush"><div className="table-wrap"><table className="signal-table">
      <thead><tr><th>时间</th><th>策略</th><th>代码</th><th>方向</th><th className="numeric">强度</th><th>市场周期</th><th>题材与角色</th><th>龙虎榜资金</th><th>封板行为</th><th>触发依据</th><th>状态</th><th className="actions-column">决策</th></tr></thead>
      <tbody>{signals.data.map((signal) => <tr key={signal.signal_id}><td>{time(signal.generated_at)}</td><td>{strategyNames[signal.strategy_id] ?? signal.strategy_id}{signal.playbook_id && <small className="subline">{playbookNames[signal.playbook_id] ?? signal.playbook_id}</small>}</td><td className="symbol">{signal.code}</td><td className={signal.side === 'BUY' ? 'positive-text' : 'negative-text'}>{signal.side}</td><td className="numeric">{percent(signal.strength)}</td><SignalContext signal={signal} /><td><div className="reason-list">{signal.reason_codes.map((reason) => <span key={reason}>{reasonNames[reason] ?? reason}</span>)}</div></td><td><StatusBadge status={signal.status} /></td><td className="actions-column">{signal.status === 'PROPOSED' && <div className="icon-actions"><button className="icon-button icon-button--approve" title="批准候选" onClick={() => setSelected({ signal, decision: 'APPROVED' })}><Check size={17} /></button><button className="icon-button" title="拒绝候选" onClick={() => setSelected({ signal, decision: 'REJECTED' })}><X size={17} /></button></div>}</td></tr>)}</tbody>
    </table></div></section>}
    {decision.error && <div className="toast toast--error">{decision.error.message}</div>}
    {selected && <DecisionDialog key={`${selected.signal.signal_id}-${selected.decision}`} signal={selected.signal} decision={selected.decision} pending={decision.isPending} onClose={() => setSelected(null)} onConfirm={(payload) => decision.mutate({ id: selected.signal.signal_id, payload })} />}
  </>
}
