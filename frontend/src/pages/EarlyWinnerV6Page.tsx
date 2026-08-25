import { useQuery } from '@tanstack/react-query'
import { DatabaseZap, FileLock2, LockKeyhole, ShieldAlert } from 'lucide-react'
import { api } from '../api'
import { ErrorState, LoadingState, PageHeader, StatusBadge } from '../components'

function yearRange(years: number[]) {
  if (years.length === 0) return '—'
  if (years.length === 1) return String(years[0])
  return `${years[0]}–${years.at(-1)}`
}

export default function EarlyWinnerV6Page() {
  const project = useQuery({
    queryKey: ['early-winner-v6'],
    queryFn: api.earlyWinnerV6,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  if (project.isLoading) return <LoadingState />
  if (project.error) return <ErrorState error={project.error} />

  const data = project.data!
  const dependency = data.protocol.dependency_lock
  const frozen = data.protocol.frozen_open
  const master = data.historical_universe_master

  return <>
    <PageHeader title="早期强势股识别 V6 · 密封事件验证" />

    <section className="metrics-band metrics-band--compact">
      <div><span>项目状态</span><StatusBadge status={data.status} /></div>
      <div><span>生命周期</span><StatusBadge status="RESEARCH_ONLY" label="永久研究项目" /></div>
      <div><span>设计区</span><strong>{yearRange(data.design_years)}</strong></div>
      <div><span>冻结验证</span><StatusBadge status="SEALED" label={data.frozen_validation_years.join('/')} /></div>
      <div><span>观察区</span><StatusBadge status="OBSERVATION_ONLY" label={yearRange(data.observation_years)} /></div>
      <div><span>冻结账簿</span><StatusBadge status={data.frozen_open_state} /></div>
    </section>

    <section className="panel early-winner-safety">
      <div><LockKeyhole size={18} /><span><strong>只读密封状态</strong><small>本页不提供开启、训练、验证或交易操作，也不会轮询或读取冻结分片。</small></span></div>
      <div><ShieldAlert size={18} /><span><strong>V5：{data.v5_disposition.status}</strong><small>V5 结果保持不可变；V6 只修复预注册与揭盲审计闭环。</small></span></div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>V5 否决依据</h2><span>原协议不回写，任何后续协议变化必须建立 {data.protocol.assessment.any_change_requires}</span></div>
      <div className="early-winner-gates">
        {data.v5_disposition.reasons.map((reason) => <article key={reason}>
          <StatusBadge status="PREREGISTRATION_REJECTED" label="已封存" />
          <strong className="symbol">{reason}</strong>
          <p>该缺口由 V6 协议修复，不改变 V5 已保存的研究证据。</p>
        </article>)}
      </div>
    </section>

    <section className="table-section">
      <div className="section-heading"><h2>数据与冻结门禁</h2><span>当前状态只来自元数据、历史母表门禁和 SQLite 冻结账簿</span></div>
      <div className="early-winner-gates">
        <article>
          <DatabaseZap size={18} />
          <StatusBadge status={master.ready ? 'READY' : master.status ?? 'BLOCKED_DATA'} />
          <strong>历史证券母表</strong>
          <p>{master.detail ?? '尚无母表审计说明'}</p>
          <small>{master.coverage_start ?? '—'} → {master.coverage_end ?? '—'}</small>
        </article>
        <article>
          <FileLock2 size={18} />
          <StatusBadge status={data.frozen_open_state} />
          <strong>一次性揭盲</strong>
          <p>{frozen.database_state_machine.join(' → ')}</p>
          <small>{frozen.one_open_only ? '数据库 CAS 只允许一次消费' : '一次性门禁未锁定'}</small>
        </article>
        <article>
          <FileLock2 size={18} />
          <strong>分片内容绑定</strong>
          <p>{frozen.per_shard_binding.join(' · ')}</p>
          <small>{frozen.path_policy}</small>
        </article>
        <article>
          <DatabaseZap size={18} />
          <strong>结果 CAS</strong>
          <p>{data.protocol.assessment.result_artifact}</p>
          <small>逐行排名证据与逐周期成交证据重新计算门禁，不信任汇总值。</small>
        </article>
      </div>
    </section>

    <section className="panel early-winner-safety">
      <div><FileLock2 size={18} /><span><strong>协议哈希</strong><small className="symbol">{data.protocol_hash}</small></span></div>
      <div><FileLock2 size={18} /><span><strong>评估器 bundle</strong><small className="symbol">{dependency.evaluator_bundle_hash}</small></span></div>
      <div><FileLock2 size={18} /><span><strong>标签 schema</strong><small className="symbol">{dependency.label_schema_hash}</small></span></div>
      <div><FileLock2 size={18} /><span><strong>依赖锁</strong><small className="symbol">{dependency.dependency_lock_hash}</small></span></div>
    </section>
  </>
}
