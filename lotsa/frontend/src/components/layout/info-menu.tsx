import { Info } from 'lucide-react'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'

export function InfoMenu() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Privacy, terms, and credits">
          <Info className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem asChild>
          <a href="/privacy" target="_blank" rel="noopener noreferrer">
            Privacy Policy
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href="/terms" target="_blank" rel="noopener noreferrer">
            Terms of Use
          </a>
        </DropdownMenuItem>
        <DropdownMenuItem asChild>
          <a href="https://andrewcrookston.com/?ref=lotsa" target="_blank" rel="noopener noreferrer">
            Built by Andrew Crookston
          </a>
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}
