"""Terminal notification for blocked items."""

from __future__ import annotations

import sys

from rigg.models import BlockingReason


class ConsoleNotifier:
    """Prints blocking reasons to stderr.

    Implements the rigg ``Notifier`` protocol.
    """

    async def notify(self, item_id: str, reason: BlockingReason) -> None:
        """Print a formatted blocking notification."""
        print(
            f"\n[BLOCKED] {item_id}\n"
            f"  Code:    {reason.code}\n"
            f"  Title:   {reason.title}\n"
            f"  Message: {reason.message}\n",
            file=sys.stderr,
        )
