// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import EarlyWinnerTradingPage from './pages/EarlyWinnerTradingPage'

describe('paper-only build', () => {
  it('exposes no real broker controls', () => {
    render(<EarlyWinnerTradingPage />)

    expect(screen.getByText('真实交易未编译')).toBeInTheDocument()
    expect(screen.getByText(/没有真实券商查询、批准、下单、撤单或恢复入口/)).toBeInTheDocument()
    expect(screen.queryByRole('button')).not.toBeInTheDocument()
  })
})
