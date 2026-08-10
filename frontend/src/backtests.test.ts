import { describe, expect, it } from 'vitest'
import {
  backtestValidation,
  backtestCostLabel,
  compressStates,
  parseStockCodes,
  strategyCatalogDisplayName,
} from './pages/BacktestsPage'

describe('backtest request helpers', () => {
  it('parses custom stock codes from common separators', () => {
    expect(parseStockCodes('600519, 000001.SZ\n300750；688001.SH')).toEqual([
      '600519',
      '000001.SZ',
      '300750',
      '688001.SH',
    ])
  })

  it('compresses consecutive identical market states', () => {
    const states = ['2026-01-05', '2026-01-06'].map((timestamp) => ({
      strategy_id: 'pairs_arbitrage_v1', timestamp, market_phase: 'PAIR_RESEARCH',
      market_style: 'MARKET_NEUTRAL', suitability: 1, trade_mode: 'PAIR_MEAN_REVERSION',
      entry_allowed: 0, state: {},
    }))
    expect(compressStates(states)).toEqual([
      expect.objectContaining({ timestamp: '2026-01-05', end_timestamp: '2026-01-06', days: 2 }),
    ])
  })

  it('does not validate annualization from a short or sparse backtest', () => {
    expect(backtestValidation(28, 6).validated).toBe(false)
    expect(backtestValidation(250, 29).validated).toBe(false)
    expect(backtestValidation(250, 30).validated).toBe(true)
  })

  it('makes rejected research strategies explicit in selectors and records', () => {
    expect(strategyCatalogDisplayName(
      '49课三次开板回封',
      'HISTORICAL_ROBUSTNESS_REJECTED',
    )).toBe('49课三次开板回封（历史稳健性否决）')
    expect(strategyCatalogDisplayName(
      '49课低拥挤回封（留出否决）',
      'HISTORICAL_REJECTED',
    )).toBe('49课低拥挤回封（留出否决）')
  })

  it('distinguishes standard and stressed execution costs', () => {
    expect(backtestCostLabel()).toBe('标准成本')
    expect(backtestCostLabel(2)).toBe('2 倍成本')
  })
})
