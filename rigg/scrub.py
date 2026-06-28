"""Redact credentials from text before it is persisted, logged, or served.

CONSTITUTION §1.2 — credentials must never appear in plaintext in the database,
in logs, or in API responses. The agent subprocess can emit a credential into
its stdout/stderr/activity (e.g. a task that runs ``env`` or ``git remote -v``),
so every path that stores or surfaces agent output runs through this scrubber.

Exact live env values are replaced first (precise), then token-shaped regexes
catch anything not in our environment (a token a task fetched from elsewhere, a
rotated value). Safe to call on any string — non-secret content is unchanged.
"""

from __future__ import annotations

import os
import re

# Credential env vars whose live values, if present, must never surface.
_SECRET_ENV_VARS = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "ANTHROPIC_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
)

# Token-shaped backstop patterns for values we can't read from the environment.
_TOKEN_PATTERNS = tuple(
    re.compile(p)
    for p in (
        r"gh[posru]_[A-Za-z0-9]{20,}",  # GitHub PAT / OAuth / server / user / refresh
        r"github_pat_[A-Za-z0-9_]{20,}",  # GitHub fine-grained PAT
        r"sk-ant-[A-Za-z0-9_-]{20,}",  # Anthropic API key
    )
)

_REDACTED = "***"


def scrub_secrets(text: str) -> str:
    """Replace known credential values and token-shaped strings with ``***``."""
    if not text:
        return text
    out = text
    for var in _SECRET_ENV_VARS:
        value = os.environ.get(var)
        # Guard the length so a short/empty value can't blank out the whole string.
        if value and len(value) >= 8:
            out = out.replace(value, _REDACTED)
    for pattern in _TOKEN_PATTERNS:
        out = pattern.sub(_REDACTED, out)
    return out
