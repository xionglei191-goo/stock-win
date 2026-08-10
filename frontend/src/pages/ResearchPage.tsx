import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BrainCircuit, Check, FlaskConical, Play, RefreshCw, ShieldAlert } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent, time } from '../components'
import type { EvidenceClaim, ResearchBrief, StrategyExperiment } from '../types'

type Workspace = 'briefs' | 'feedback' | 'experiments'

function Claim({ claim }: { claim: EvidenceClaim }) {
  return <div className="research-claim">
    <div><p>{claim.text}</p><span>置信度 {percent(claim.confidence)}</span></div>
    <div className="evidence-list">{claim.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}</div>
    {claim.limitations.length > 0 && <small>{claim.limitations.join('；')}</small>}
  </div>
}

export function BriefWorkspace({ briefs }: { briefs: ResearchBrief[] }) {
  const [selectedId, setSelectedId] = useState('')
  const latestId = selectedId || briefs[0]?.brief_id || ''
  useEffect(() => {
    if (selectedId && !briefs.some((item) => item.brief_id === selectedId)) setSelectedId('')
  }, [briefs, selectedId])
  const detail = useQuery({
    queryKey: ['research-brief', latestId],
    queryFn: () => api.researchBrief(latestId),
    enabled: Boolean(latestId),
    refetchInterval: 5_000,
  })
  const brief = detail.data
  return <div className="research-layout">
    <section className="panel research-index">
      <div className="section-heading"><h2>研究简报</h2><span>{briefs.length} 份</span></div>
      {!briefs.length ? <EmptyState /> : <div className="research-list">{briefs.map((item) => <button key={item.brief_id} className={item.brief_id === latestId ? 'active' : ''} onClick={() => setSelectedId(item.brief_id)}>
        <span><strong>{item.content?.headline || `运行 ${item.run_id.slice(0, 8)}`}</strong><small>{time(item.created_at)} · {item.model || '等待模型'}</small></span><StatusBadge status={item.status} />
      </button>)}</div>}
    </section>
    <section className="panel research-detail">
      {!latestId ? <EmptyState>完成一次扫描后生成首份简报</EmptyState> : detail.isLoading ? <LoadingState /> : detail.error ? <ErrorState error={detail.error} /> : brief?.status === 'FAILED' ? <div className="brief-failed"><ShieldAlert size={24} /><div><strong>AI 简报不可用</strong><p>{brief.error}</p><small>扫描、候选和模拟交易数据不受影响。</small></div></div> : !brief?.content ? <LoadingState /> : <>
        <div className="section-heading"><div><h2>{brief.content.headline}</h2><span>{brief.prompt_version} · {brief.input_hash.slice(0, 12)}</span></div><StatusBadge status={brief.status} /></div>
        <Claim claim={brief.content.market_summary} />
        <div className="claim-columns">
          <div><h3>策略观察</h3>{brief.content.strategy_summaries.length ? brief.content.strategy_summaries.map((claim, index) => <Claim key={index} claim={claim} />) : <EmptyState />}</div>
          <div><h3>持仓风险</h3>{brief.content.portfolio_risks.length ? brief.content.portfolio_risks.map((claim, index) => <Claim key={index} claim={claim} />) : <EmptyState />}</div>
          <div><h3>数据缺口</h3>{brief.content.data_gaps.length ? brief.content.data_gaps.map((claim, index) => <Claim key={index} claim={claim} />) : <EmptyState />}</div>
        </div>
        <div className="review-table"><div className="section-heading"><h2>候选意见</h2><span>{brief.reviews?.length ?? 0} 条</span></div>
          {!brief.reviews?.length ? <EmptyState /> : <div className="table-wrap"><table><thead><tr><th>候选</th><th>意见</th><th>置信度</th><th>摘要</th><th>证据</th></tr></thead><tbody>{brief.reviews.map((review) => <tr key={review.review_id}><td className="symbol">{review.signal_id.slice(0, 10)}</td><td><StatusBadge status={review.recommendation} /></td><td>{percent(review.confidence)}</td><td>{review.summary}</td><td><div className="evidence-list">{review.evidence_refs.map((ref) => <code key={ref}>{ref}</code>)}</div></td></tr>)}</tbody></table></div>}
        </div>
      </>}
    </section>
  </div>
}

function FeedbackWorkspace() {
  const feedback = useQuery({ queryKey: ['research-feedback'], queryFn: api.feedbackSummary, refetchInterval: 5_000 })
  if (feedback.isLoading) return <LoadingState />
  if (feedback.error) return <ErrorState error={feedback.error} />
  if (!feedback.data?.rows.length) return <section className="panel"><EmptyState>审批并刷新反馈后显示评价结果</EmptyState></section>
  return <>
    <section className="table-section table-section--flush"><div className="table-wrap"><table><thead><tr><th>策略</th><th>市场阶段</th><th>理由标签</th><th>AI 一致性</th><th>样本</th><th>5日均值</th><th>5日胜率</th></tr></thead><tbody>{feedback.data.aggregates.map((item) => <tr key={`${item.strategy_id}-${item.market_phase}-${item.reason_tag}-${item.ai_alignment}`}><td>{item.strategy_id}</td><td>{item.market_phase}</td><td>{item.reason_tag}</td><td>{item.ai_alignment}</td><td>{item.sample_size} {!item.sufficient_sample && <StatusBadge status="INSUFFICIENT" label="样本不足" />}</td><td>{item.average_return_5d === null ? '-' : percent(item.average_return_5d)}</td><td>{item.win_rate_5d === null ? '-' : percent(item.win_rate_5d)}</td></tr>)}</tbody></table></div></section>
    <section className="table-section table-section--flush"><div className="table-wrap"><table><thead><tr><th>信号</th><th>口径</th><th>状态</th><th>1日</th><th>3日</th><th>5日</th><th>MAE / MFE</th><th>阻断原因</th></tr></thead><tbody>{feedback.data.rows.map((row) => <tr key={row.outcome_id}><td className="symbol">{row.signal_id.slice(0, 12)}</td><td>{row.basis}</td><td><StatusBadge status={row.status} /></td><td>{row.return_1d === null ? '-' : percent(row.return_1d)}</td><td>{row.return_3d === null ? '-' : percent(row.return_3d)}</td><td>{row.return_5d === null ? '-' : percent(row.return_5d)}</td><td>{row.mae === null ? '-' : `${percent(row.mae)} / ${percent(row.mfe ?? 0)}`}</td><td>{row.block_reason || '-'}</td></tr>)}</tbody></table></div></section>
  </>
}

function metric(value: number | undefined) {
  return value === undefined ? '-' : percent(value)
}

function ExperimentRow({ experiment, onPromote, promoting }: { experiment: StrategyExperiment; onPromote: () => void; promoting: boolean }) {
  const gates = experiment.validation?.gates ?? {}
  const statistical = experiment.validation?.statistical_validation as { status?: string; policy_freeze_date?: string; failed_checks?: string[] } | undefined
  const hooks = experiment.validation?.overridden_hooks as string[] | undefined
  return <section className="experiment-row">
    <div className="experiment-title"><div><strong>{experiment.hypothesis}</strong><span>{time(experiment.created_at)} · 基准 {experiment.baseline_backtest_id.slice(0, 10)}</span></div><StatusBadge status={experiment.status} /></div>
    {experiment.summary && <p>{experiment.summary}</p>}
    <div className="experiment-metrics"><span>基准收益 <strong>{metric(experiment.baseline_metrics?.total_return)}</strong></span><span>候选收益 <strong>{metric(experiment.candidate_metrics?.total_return)}</strong></span><span>候选回撤 <strong>{metric(experiment.candidate_metrics?.max_drawdown)}</strong></span><span>双倍成本 <strong>{metric(experiment.stress_metrics?.total_return)}</strong></span></div>
    {Object.keys(gates).length > 0 && <div className="gate-list">{Object.entries(gates).map(([name, passed]) => <span className={passed ? 'passed' : 'failed'} key={name}>{passed ? <Check size={13} /> : <ShieldAlert size={13} />}{name}</span>)}</div>}
    {(hooks?.length || statistical) && <div className="experiment-audit"><span>覆盖 hook：{hooks?.join('、') || '-'}</span>{statistical && <span>统计验证：<StatusBadge status={statistical.status || 'UNVERIFIED'} /> · 冻结日 {statistical.policy_freeze_date || '-'} · 未通过 {statistical.failed_checks?.length ?? 0} 项</span>}</div>}
    {experiment.error && <div className="form-error">{experiment.error}</div>}
    <div className="experiment-footer"><code>{experiment.source_hash?.slice(0, 16) || '尚未生成源码'}</code>{experiment.status === 'READY' && <button className="button" disabled={promoting} onClick={onPromote}><Check size={16} />人工晋级为仅回测</button>}</div>
  </section>
}

function ExperimentWorkspace() {
  const queryClient = useQueryClient()
  const [baseline, setBaseline] = useState('')
  const [hypothesis, setHypothesis] = useState('')
  const backtests = useQuery({ queryKey: ['backtests'], queryFn: api.backtests })
  const experiments = useQuery({ queryKey: ['research-experiments'], queryFn: api.experiments, refetchInterval: 5_000 })
  const baselines = useMemo(() => backtests.data?.filter((item) => item.strategy_id === 'course49_v3' && item.status === 'SUCCEEDED' && item.snapshot_id) ?? [], [backtests.data])
  useEffect(() => { if (!baseline && baselines[0]) setBaseline(baselines[0].backtest_id) }, [baseline, baselines])
  const create = useMutation({
    mutationFn: () => api.createExperiment(baseline, hypothesis),
    onSuccess: () => { setHypothesis(''); queryClient.invalidateQueries({ queryKey: ['research-experiments'] }) },
  })
  const promote = useMutation({ mutationFn: api.promoteExperiment, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['research-experiments'] }) })
  return <>
    <section className="panel experiment-form"><div className="section-heading"><h2>创建 V3 隔离实验</h2><span>只允许覆盖批准的纯函数 hook</span></div><div className="experiment-form-grid">
      <label className="field"><span>不可变基准回测</span><select value={baseline} onChange={(event) => setBaseline(event.target.value)}><option value="">选择成功的 V3 回测</option>{baselines.map((item) => <option key={item.backtest_id} value={item.backtest_id}>{item.backtest_id.slice(0, 12)} · {item.start_date}–{item.end_date}</option>)}</select></label>
      <label className="field"><span>实验假设</span><textarea rows={3} minLength={10} maxLength={2000} value={hypothesis} onChange={(event) => setHypothesis(event.target.value)} placeholder="例如：提高强封板且机构净买候选的排序权重，同时降低高位接力目标仓位。" /></label>
      <button className="button" disabled={!baseline || hypothesis.trim().length < 10 || create.isPending} onClick={() => create.mutate()}><FlaskConical size={16} />生成并验证</button>
    </div>{create.error && <div className="form-error">{create.error.message}</div>}</section>
    <div className="experiment-list">{experiments.isLoading ? <LoadingState /> : experiments.error ? <ErrorState error={experiments.error} /> : !experiments.data?.length ? <section className="panel"><EmptyState /></section> : experiments.data.map((item) => <ExperimentRow key={item.experiment_id} experiment={item} promoting={promote.isPending} onPromote={() => promote.mutate(item.experiment_id)} />)}</div>
    {promote.error && <div className="toast toast--error">{promote.error.message}</div>}
  </>
}

export default function ResearchPage() {
  const [workspace, setWorkspace] = useState<Workspace>('briefs')
  const queryClient = useQueryClient()
  const briefs = useQuery({ queryKey: ['research-briefs'], queryFn: api.researchBriefs, refetchInterval: 5_000 })
  const runDaily = useMutation({ mutationFn: api.runDailyResearch, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['research-briefs'] }) })
  const regenerate = useMutation({ mutationFn: api.generateBrief, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['research-briefs'] }) })
  const refresh = useMutation({ mutationFn: api.refreshFeedback, onSuccess: () => queryClient.invalidateQueries({ queryKey: ['research-feedback'] }) })
  const latestRun = briefs.data?.[0]?.run_id
  const actionError = runDaily.error || regenerate.error || refresh.error
  return <>
    <PageHeader title="研究助手" actions={<>
      {workspace === 'briefs' && <><button className="button" disabled={runDaily.isPending} onClick={() => runDaily.mutate()}><Play size={16} />扫描并生成简报</button><button className="button button--secondary" disabled={!latestRun || regenerate.isPending} onClick={() => latestRun && regenerate.mutate(latestRun)}><RefreshCw size={16} />基于已有运行重新生成</button></>}
      {workspace === 'feedback' && <button className="button" disabled={refresh.isPending} onClick={() => refresh.mutate()}><RefreshCw size={16} />刷新决策反馈</button>}
    </>} />
    <div className="research-tabs segmented"><button className={workspace === 'briefs' ? 'active' : ''} onClick={() => setWorkspace('briefs')}><BrainCircuit size={15} />每日简报</button><button className={workspace === 'feedback' ? 'active' : ''} onClick={() => setWorkspace('feedback')}><RefreshCw size={15} />决策反馈</button><button className={workspace === 'experiments' ? 'active' : ''} onClick={() => setWorkspace('experiments')}><FlaskConical size={15} />V3 实验室</button></div>
    {workspace === 'briefs' && (briefs.isLoading ? <LoadingState /> : briefs.error ? <ErrorState error={briefs.error} /> : <BriefWorkspace briefs={briefs.data ?? []} />)}
    {workspace === 'feedback' && <FeedbackWorkspace />}
    {workspace === 'experiments' && <ExperimentWorkspace />}
    {actionError && <div className="toast toast--error">{actionError.message}</div>}
  </>
}
