import { describe, expect, it } from 'vitest'
import { boardSummary, capitalSummary, marketSummary, themeSummary } from './pages/SignalsPage'

describe('course49 signal evidence helpers', () => {
  it('keeps missing LHB data distinct from capital outflow', () => {
    expect(capitalSummary({ lhb: { listed: false } })).toEqual({
      state: '未上榜', net: '-', institution: '-', risk: false,
    })
    expect(capitalSummary({ lhb: { listed: true, event_date: '2026-07-20', net_buy_ratio: -0.1, risk: 'LHB_DISTRIBUTION' } }).risk).toBe(true)
  })

  it('translates cycle and leader evidence for research display', () => {
    expect(marketSummary({ market_phase: 'FERMENT', market_score: 0.7 })).toEqual({
      phase: '发酵', score: '70%', style: '-', mode: '-',
    })
    expect(marketSummary({ market_phase: 'FERMENT', market_score: 0.7, market_style: 'MIXED', style_suitability: 0.55, trade_mode: 'FERMENT_SECOND_BOARD' })).toEqual({
      phase: '发酵', score: '70%', style: '混合风格 55%', mode: '发酵二板',
    })
    expect(themeSummary({ sector_name: '机器人', theme_phase: 'START', role: 'SPACE_LEADER', limit_streak: 2 })).toEqual({
      sector: '机器人', phase: '启动', role: '空间龙头', streak: '2板',
    })
  })

  it('shows historical seal quality without treating missing data as weak quality', () => {
    expect(boardSummary({ limit_behavior: { limit_event: false } })).toEqual({
      state: '无涨停行为', detail: '-', risk: false,
    })
    expect(boardSummary({ limit_behavior: {
      limit_event: true, first_limit_time: '094000', open_board_count: 0, board_quality_score: 0.82,
    } })).toEqual({ state: '质量 82%', detail: '首封 09:40 · 开板 0 次', risk: false })
  })
})
