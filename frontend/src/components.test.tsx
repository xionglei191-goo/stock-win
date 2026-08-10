// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest'
import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { StatusBadge } from './components'

describe('StatusBadge', () => {
  it('renders operational status text', () => {
    render(<StatusBadge status="READY" />)
    expect(screen.getByText('READY')).toHaveClass('status--positive')
  })
})
