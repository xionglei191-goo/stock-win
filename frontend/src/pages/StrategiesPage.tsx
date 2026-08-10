import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, FlaskConical, Play, Plus, RefreshCw, Save, Trash2, X } from 'lucide-react'
import { api } from '../api'
import { EmptyState, ErrorState, LoadingState, PageHeader, StatusBadge, percent, time } from '../components'
import type { StrategyGroup, StrategyGroupDraft, StrategyGroupMember } from '../types'

const emptyGroup = (): StrategyGroupDraft => ({
  group_id: '',
  version: '1.0.0',
  name: '',
  description: '',
  composition_mode: 'capital_sleeves',
  conflict_policy: 'risk_first',
  enabled: true,
  members: [],
})

export function groupWeightValid(group: Pick<StrategyGroup, 'composition_mode' | 'members'>) {
  if (!group.members.length || group.members.some((item) => item.weight <= 0)) return false
  if (group.composition_mode === 'capital_sleeves') {
    return Math.abs(group.members.reduce((sum, item) => sum + item.weight, 0) - 1) < 0.0001
  }
  const alpha = group.members.filter((item) => item.role === 'alpha').length
  const risk = group.members.filter((item) => item.role === 'risk').length
  if (group.composition_mode === 'risk_overlay') return alpha >= 1 && risk >= 1
  return alpha >= 2 && risk === 0
}

export function capabilityLabel(scanSupported: boolean, backtestSupported: boolean) {
  if (scanSupported && backtestSupported) return '扫描 / 回测'
  if (scanSupported) return '仅扫描'
  if (backtestSupported) return '仅回测'
  return '不可运行'
}

export function lifecycleLabel(value: string) {
  if (value === 'HISTORICAL_REJECTED') return '历史留出否决'
  if (value === 'HISTORICAL_ROBUSTNESS_REJECTED') return '历史稳健性否决'
  if (value === 'HOLDOUT_TARGET_REJECTED') return '留出收益目标否决'
  if (value === 'EXPERIMENTAL') return '实验'
  return value === 'ACTIVE' ? '有效' : value
}

export function lifecycleStatus(value: string) {
  return value.endsWith('_REJECTED') ? 'REJECTED' : value
}

export default function StrategiesPage() {
  const queryClient = useQueryClient()
  const catalog = useQuery({ queryKey: ['strategy-catalog'], queryFn: api.strategyCatalog })
  const orderGroups = useQuery({ queryKey: ['order-groups'], queryFn: api.orderGroups, refetchInterval: 20_000 })
  const [draft, setDraft] = useState<StrategyGroupDraft>(emptyGroup)
  const [editingBuiltIn, setEditingBuiltIn] = useState(false)
  const [showArchived, setShowArchived] = useState(false)
  const totalWeight = useMemo(() => draft.members.reduce((sum, item) => sum + item.weight, 0), [draft.members])
  const save = useMutation({
    mutationFn: api.saveStrategyGroup,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['strategy-catalog'] })
      setDraft(emptyGroup())
      setEditingBuiltIn(false)
    },
  })
  const remove = useMutation({
    mutationFn: api.deleteStrategyGroup,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['strategy-catalog'] }),
  })
  const scan = useMutation({ mutationFn: (id: string) => api.scanSelection([id]) })
  const reload = useMutation({
    mutationFn: api.reloadStrategyCatalog,
    onSuccess: (value) => queryClient.setQueryData(['strategy-catalog'], value),
  })
  const decide = useMutation({
    mutationFn: ({ id, decision }: { id: string; decision: 'APPROVED' | 'REJECTED' }) => api.decideOrderGroup(id, decision),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['order-groups'] }),
  })
  if (catalog.isLoading || orderGroups.isLoading) return <LoadingState />
  if (catalog.error) return <ErrorState error={catalog.error} />
  if (orderGroups.error) return <ErrorState error={orderGroups.error} />
  const data = catalog.data!

  const editGroup = (group: StrategyGroup) => {
    setDraft({
      group_id: group.group_id,
      version: group.version,
      name: group.name,
      description: group.description,
      composition_mode: group.composition_mode === 'comparison' ? 'capital_sleeves' : group.composition_mode,
      conflict_policy: group.conflict_policy,
      enabled: group.enabled,
      members: group.members.map((item) => ({ ...item })),
    })
    setEditingBuiltIn(group.built_in)
  }
  const toggleMember = (strategyId: string) => {
    const exists = draft.members.some((item) => item.strategy_id === strategyId)
    const members = exists
      ? draft.members.filter((item) => item.strategy_id !== strategyId)
      : [...draft.members, { strategy_id: strategyId, weight: 0.25, role: 'alpha', priority: (draft.members.length + 1) * 10 } as StrategyGroupMember]
    setDraft({ ...draft, members })
  }
  const updateMember = (strategyId: string, values: Partial<StrategyGroupMember>) => {
    setDraft({ ...draft, members: draft.members.map((item) => item.strategy_id === strategyId ? { ...item, ...values } : item) })
  }

  return <>
    <PageHeader title="策略实验室" actions={<><button className="icon-button" title="重新加载本地策略插件" disabled={reload.isPending} onClick={() => reload.mutate()}><RefreshCw size={17} /></button><button className="button button--secondary" onClick={() => { setDraft(emptyGroup()); setEditingBuiltIn(false) }}><Plus size={17} />新建组合</button></>} />
    <section className="metrics-band metrics-band--compact">
      <div><span>正式策略</span><strong>{data.strategies.length}</strong></div>
      <div><span>策略组合</span><strong>{data.groups.length}</strong></div>
      <div><span>多腿策略</span><strong>{data.strategies.filter((item) => item.execution_model === 'MULTI_LEG').length}</strong></div>
      <div><span>待批多腿</span><strong>{orderGroups.data!.filter((item) => item.status === 'PROPOSED').length}</strong></div>
      <div><span>加载问题</span><strong>{data.plugin_issues.length}</strong></div>
      <div><span>研究归档</span><strong>{data.archived_strategies?.length ?? 0}</strong></div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>策略目录</h2><button className="button button--secondary" type="button" onClick={() => setShowArchived((value) => !value)}>{showArchived ? '隐藏研究归档' : '显示研究归档'}</button></div>
      <div className="table-wrap"><table className="strategy-catalog-table"><thead><tr><th>策略</th><th>版本</th><th>状态</th><th>来源</th><th>频率</th><th>执行</th><th>资产</th><th>数据需求</th><th>审批</th></tr></thead><tbody>
        {data.strategies.map((strategy) => <tr key={strategy.strategy_id}><td><strong>{strategy.name}</strong><small className="subline">{strategy.strategy_id}</small></td><td>{strategy.version}<small className="subline">API {strategy.plugin_api_version}</small></td><td><StatusBadge status={lifecycleStatus(strategy.lifecycle)} label={lifecycleLabel(strategy.lifecycle)} /></td><td>{strategy.plugin_origin}<small className="subline">{strategy.runtime_adapter}</small></td><td>{strategy.frequency}</td><td>{strategy.execution_model === 'MULTI_LEG' ? '多腿' : '单腿'}{strategy.supports_short ? ' · 可做空' : ''}<small className="subline">{capabilityLabel(strategy.scan_enabled, strategy.backtest_enabled)}</small></td><td>{strategy.asset_classes.join(' / ')}</td><td><div className="requirement-list">{strategy.data_requirements.map((item, index) => <span key={`${item.dataset}-${index}`} className={item.available === false ? 'negative-text' : ''}>{item.dataset} · {item.frequency} · {item.adjustment} · {item.provider ?? '-'}</span>)}</div></td><td><StatusBadge status={strategy.requires_approval ? 'REQUIRED' : 'AUTO'} label={strategy.requires_approval ? '需审批' : '自动'} /></td></tr>)}
      </tbody></table></div>
    </section>

    {showArchived && <section className="table-section">
      <div className="section-heading"><h2>研究归档</h2><span>保留直接回测，不参与普通扫描</span></div>
      <div className="table-wrap"><table><thead><tr><th>策略</th><th>版本</th><th>历史状态</th><th>能力</th><th>规则族</th></tr></thead><tbody>
        {(data.archived_strategies ?? []).map((strategy) => <tr key={strategy.strategy_id}>
          <td><strong>{strategy.name}</strong><small className="subline">{strategy.strategy_id}</small></td>
          <td>{strategy.version}</td>
          <td><StatusBadge status="ARCHIVED" label={lifecycleLabel(strategy.lifecycle)} /></td>
          <td>{capabilityLabel(strategy.scan_enabled, strategy.backtest_enabled)}</td>
          <td className="symbol">{strategy.strategy_family}</td>
        </tr>)}
      </tbody></table></div>
    </section>}

    {data.plugin_issues.length > 0 && <section className="table-section">
      <div className="section-heading"><h2>目录加载问题</h2><span>{data.plugin_issues.length} 项</span></div>
      <div className="table-wrap"><table><thead><tr><th>插件</th><th>错误</th><th>位置</th><th>详情</th></tr></thead><tbody>{data.plugin_issues.map((issue) => <tr key={`${issue.origin}-${issue.plugin_id}`}><td className="symbol">{issue.plugin_id}</td><td><StatusBadge status="FAILED" label={issue.code} /></td><td>{issue.origin}</td><td>{issue.message}</td></tr>)}</tbody></table></div>
    </section>}

    <div className="strategy-workbench">
      <section className="panel strategy-group-list">
        <div className="section-heading"><h2>组合配置</h2><span>{data.groups.length} 组</span></div>
        {data.groups.map((group) => <div key={group.group_id} className={`strategy-group-row ${draft.group_id === group.group_id ? 'active' : ''}`}>
          <button className="strategy-group-select" onClick={() => editGroup(group)}><span><strong>{group.name}</strong><small>{group.composition_mode} · {capabilityLabel(group.scan_supported, group.backtest_supported)} · {group.version}</small></span></button>
          <span className="strategy-group-actions"><button className="icon-button" title={group.scan_supported ? '运行组合扫描' : '该组合仅允许回测'} disabled={!group.scan_supported} onClick={(event) => { event.stopPropagation(); scan.mutate(group.group_id) }}><Play size={15} /></button>{!group.built_in && <button className="icon-button" title="删除组合" onClick={(event) => { event.stopPropagation(); remove.mutate(group.group_id) }}><Trash2 size={15} /></button>}</span>
        </div>)}
      </section>

      <section className="panel strategy-editor">
        <div className="section-heading"><h2>{draft.group_id ? '组合定义' : '新组合'}</h2><span>{draft.composition_mode === 'capital_sleeves' ? `资金 ${percent(totalWeight)}` : `${draft.members.length} 个成员`}</span></div>
        <div className="strategy-form-grid">
          <label className="field"><span>组合 ID</span><input value={draft.group_id} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, group_id: event.target.value })} placeholder="my_strategy_group" /></label>
          <label className="field"><span>名称</span><input value={draft.name} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>
          <label className="field"><span>版本</span><input value={draft.version} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, version: event.target.value })} /></label>
          <label className="field"><span>组合模式</span><select value={draft.composition_mode} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, composition_mode: event.target.value as StrategyGroup['composition_mode'] })}><option value="capital_sleeves">独立资金分舱</option><option value="score_fusion">信号加权</option><option value="intersection">交集确认</option><option value="risk_overlay">风险覆盖</option></select></label>
          <label className="field"><span>冲突处理</span><select value={draft.conflict_policy} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, conflict_policy: event.target.value as StrategyGroup['conflict_policy'] })}><option value="risk_first">风险优先</option><option value="net_score">净分数</option><option value="priority">成员优先级</option></select></label>
          <label className="field field--description"><span>说明</span><input value={draft.description} disabled={editingBuiltIn} onChange={(event) => setDraft({ ...draft, description: event.target.value })} /></label>
        </div>
        <div className="member-editor">
          {data.strategies.map((strategy) => {
            const member = draft.members.find((item) => item.strategy_id === strategy.strategy_id)
            return <div key={strategy.strategy_id} className={`member-row ${member ? 'active' : ''}`}>
              <label className="member-toggle"><input type="checkbox" checked={Boolean(member)} disabled={editingBuiltIn} onChange={() => toggleMember(strategy.strategy_id)} /><span><strong>{strategy.name}</strong><small>{strategy.execution_model === 'MULTI_LEG' ? '多腿' : strategy.frequency}</small></span></label>
              {member && <><label className="compact-field"><span>权重</span><input type="number" min="0.01" max="1" step="0.05" disabled={editingBuiltIn} value={member.weight} onChange={(event) => updateMember(strategy.strategy_id, { weight: Number(event.target.value) })} /></label><label className="compact-field"><span>角色</span><select disabled={editingBuiltIn} value={member.role} onChange={(event) => updateMember(strategy.strategy_id, { role: event.target.value as 'alpha' | 'risk' })}><option value="alpha">Alpha</option><option value="risk">风险</option></select></label></>}
            </div>
          })}
        </div>
        {!editingBuiltIn && <div className="editor-actions"><button className="button" disabled={!draft.group_id || !draft.name || !groupWeightValid(draft) || save.isPending} onClick={() => save.mutate(draft)}><Save size={17} />保存组合</button></div>}
        {(save.error || remove.error || scan.error || reload.error) && <div className="form-error">{(save.error ?? remove.error ?? scan.error ?? reload.error)?.message}</div>}
      </section>
    </div>

    <section className="table-section">
      <div className="section-heading"><h2>多腿意图</h2><span>{orderGroups.data!.length} 条</span></div>
      {orderGroups.data!.length ? <div className="table-wrap"><table className="order-group-table"><thead><tr><th>时间</th><th>策略</th><th>组合</th><th>动作</th><th>交易腿</th><th>总敞口</th><th>状态</th><th></th></tr></thead><tbody>
        {orderGroups.data!.map((intent) => <tr key={intent.intent_id}><td>{time(intent.generated_at)}</td><td>{intent.strategy_id}</td><td className="symbol">{intent.group_key}</td><td>{intent.action}</td><td><div className="leg-list">{intent.legs.map((leg) => <span key={leg.leg_id} className={leg.side === 'BUY' || leg.side === 'COVER' ? 'positive-text' : 'negative-text'}>{leg.side} {leg.code}</span>)}</div></td><td>{percent(intent.gross_target_weight)}</td><td><StatusBadge status={intent.status} /></td><td>{intent.status === 'PROPOSED' && <div className="icon-actions"><button className="icon-button icon-button--approve" title="批准整组" onClick={() => decide.mutate({ id: intent.intent_id, decision: 'APPROVED' })}><Check size={16} /></button><button className="icon-button" title="拒绝整组" onClick={() => decide.mutate({ id: intent.intent_id, decision: 'REJECTED' })}><X size={16} /></button></div>}</td></tr>)}
      </tbody></table></div> : <EmptyState />}
    </section>
    {(save.data || scan.data) && <div className="toast"><FlaskConical size={15} />{scan.data ? `扫描任务 ${scan.data.job_id.slice(0, 8)}` : '组合已保存'}</div>}
  </>
}
