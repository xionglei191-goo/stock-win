import type { ReactNode } from 'react'
import { AlertTriangle, CheckCircle2, LoaderCircle } from 'lucide-react'

export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const normalized = status.toUpperCase()
  const tone = ['READY', 'DATA_READY', 'SUCCEEDED', 'APPROVED', 'EXECUTED', 'PAPER_QUALIFIED'].includes(normalized)
    ? 'positive'
    : ['FAILED', 'WEAK', 'STALE', 'REJECTED', 'DATA_BLOCKED', 'PAPER_BLOCKED', 'DATA_DEGRADED', 'KILLED', 'CRITICAL', 'HIGH'].includes(normalized)
      ? 'negative'
      : 'warning'
  return <span className={`status status--${tone}`}>{label ?? status}</span>
}

export function LoadingState() {
  return <div className="state-line"><LoaderCircle className="spin" size={18} /> 正在读取</div>
}

export function ErrorState({ error }: { error: Error }) {
  return <div className="error-line"><AlertTriangle size={18} /> {error.message}</div>
}

export function EmptyState({ children = '暂无记录' }: { children?: ReactNode }) {
  return <div className="empty-state">{children}</div>
}

export function PageHeader({ title, actions }: { title: string; actions?: ReactNode }) {
  return <div className="page-header"><h1>{title}</h1><div className="page-actions">{actions}</div></div>
}

export function HealthIcon({ ok }: { ok: boolean }) {
  return ok ? <CheckCircle2 className="icon-positive" size={18} /> : <AlertTriangle className="icon-negative" size={18} />
}

export function money(value: number) {
  return new Intl.NumberFormat('zh-CN', { style: 'currency', currency: 'CNY', maximumFractionDigits: 0 }).format(value)
}

export function percent(value: number) {
  return new Intl.NumberFormat('zh-CN', { style: 'percent', maximumFractionDigits: 1 }).format(value)
}

export function time(value?: string) {
  return value ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '-'
}
