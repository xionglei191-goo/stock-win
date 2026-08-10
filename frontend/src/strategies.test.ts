import { describe, expect, it } from 'vitest'
import {
  capabilityLabel,
  groupWeightValid,
  lifecycleLabel,
  lifecycleStatus,
} from './pages/StrategiesPage'

describe('strategy group validation', () => {
  it('requires capital sleeves to allocate exactly 100 percent', () => {
    expect(groupWeightValid({
      composition_mode: 'capital_sleeves',
      members: [
        { strategy_id: 'chan_v1', weight: 0.4, role: 'alpha', priority: 10 },
        { strategy_id: 'course49_v2', weight: 0.6, role: 'alpha', priority: 20 },
      ],
    })).toBe(true)
    expect(groupWeightValid({
      composition_mode: 'capital_sleeves',
      members: [{ strategy_id: 'chan_v1', weight: 0.4, role: 'alpha', priority: 10 }],
    })).toBe(false)
  })

  it('requires role-compatible signal combinations', () => {
    expect(groupWeightValid({
      composition_mode: 'risk_overlay',
      members: [
        { strategy_id: 'alpha', weight: 0.8, role: 'alpha', priority: 10 },
        { strategy_id: 'risk', weight: 0.2, role: 'risk', priority: 20 },
      ],
    })).toBe(true)
    expect(groupWeightValid({
      composition_mode: 'score_fusion',
      members: [
        { strategy_id: 'alpha', weight: 0.8, role: 'alpha', priority: 10 },
        { strategy_id: 'risk', weight: 0.2, role: 'risk', priority: 20 },
      ],
    })).toBe(false)
  })
})

describe('strategy capability labels', () => {
  it('does not present a backtest-only group as scannable', () => {
    expect(capabilityLabel(false, true)).toBe('仅回测')
    expect(capabilityLabel(true, true)).toBe('扫描 / 回测')
  })

  it('renders every rejected research lifecycle as a rejection', () => {
    expect(lifecycleLabel('HISTORICAL_ROBUSTNESS_REJECTED')).toBe('历史稳健性否决')
    expect(lifecycleLabel('HOLDOUT_TARGET_REJECTED')).toBe('留出收益目标否决')
    expect(lifecycleStatus('HISTORICAL_ROBUSTNESS_REJECTED')).toBe('REJECTED')
    expect(lifecycleStatus('ACTIVE')).toBe('ACTIVE')
  })
})
