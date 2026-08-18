import { describe, it, expect } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { InfoMenu } from '@/components/layout/info-menu'

describe('InfoMenu', () => {
  it('opens to reveal Privacy, Terms, and Credits links with the right hrefs', async () => {
    render(<InfoMenu />)

    const trigger = screen.getByRole('button', { name: /privacy, terms, and credits/i })
    // Radix's DropdownMenuTrigger opens on `pointerdown`, not `click` — see
    // the PointerEvent polyfill in src/test/setup.ts for why this needs one.
    fireEvent.pointerDown(trigger, { button: 0, ctrlKey: false, pointerId: 1 })

    await waitFor(() => screen.getByRole('menuitem', { name: 'Privacy Policy' }))

    expect(screen.getByRole('menuitem', { name: 'Privacy Policy' })).toHaveAttribute(
      'href',
      '/privacy',
    )
    expect(screen.getByRole('menuitem', { name: 'Terms of Use' })).toHaveAttribute(
      'href',
      '/terms',
    )
    expect(screen.getByRole('menuitem', { name: 'Built by Andrew Crookston' })).toHaveAttribute(
      'href',
      'https://andrewcrookston.com/?ref=lotsa',
    )
  })
})
