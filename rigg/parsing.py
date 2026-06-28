"""Parsing utilities for Claude CLI JSON output.

The Claude CLI with ``--output-format json --verbose`` writes a JSON array
of event objects to stdout. Without ``--verbose``, it writes a single JSON
object. The result event contains the text response, session ID, token
usage, and cost.

``parse_claude_output`` extracts all of these into a ``ParsedOutput``
dataclass. When the stream lacks a clean result text (the agent terminated
abnormally — budget exit, timeout, parse failure), the parser falls back to
a bounded summary built from the event stream rather than returning the raw
multi-MB blob; persisting the latter to the audit log crashes the React
UI's JSON-parse path on tasks with long-running agent dispatches.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Upper bound on ``parsed.stdout`` in bytes. Chat messages are usually
# <5 KB; the cap is generous so structured summaries with long tool-call
# lists still fit, while pathological streams (1 MB+ verbose dumps) are
# bounded.
_MAX_STDOUT_BYTES = 20_000


@dataclass
class ParsedOutput:
    """Parsed fields from the Claude CLI JSON envelope."""

    stdout: str
    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None


def parse_claude_output(raw: str) -> ParsedOutput:
    """Parse JSON output from the Claude CLI.

    With ``--verbose``, claude outputs a JSON array of event objects.
    Without ``--verbose``, it's a single JSON object. The result is in
    the object with ``type="result"``.

    When the stream lacks a clean result text (no result event, or a
    result event missing the ``result`` field — the budget-exit shape),
    the parser walks the events and returns a structured summary
    (assistant text + tool-call counts + errors).  When JSON parsing
    fails entirely, the raw text is bounded by truncation.  In all
    cases ``parsed.stdout`` is guaranteed to fit within
    :data:`_MAX_STDOUT_BYTES`.
    """
    events: list[dict] = []
    try:
        stripped = raw.strip()
        if stripped.startswith("["):
            events = json.loads(stripped)
        elif stripped.startswith("{"):
            events = [json.loads(stripped)]
    except (json.JSONDecodeError, TypeError):
        logger.debug("Failed to parse Claude CLI output as JSON, using bounded raw stdout")

    if not events:
        return ParsedOutput(stdout=_bound(raw))

    # Find a result event if present; it carries terminal session info
    # and (usually) the agent's final text.
    result_event: dict | None = None
    for event in events:
        if isinstance(event, dict) and event.get("type") == "result":
            result_event = event
            break

    session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None

    if result_event is not None:
        session_id = result_event.get("session_id")
        v = result_event.get("total_cost_usd")
        cost_usd = v if v is not None else result_event.get("cost_usd")
        usage = result_event.get("usage")
        if isinstance(usage, dict):
            if "input_tokens" in usage:
                input_tokens = int(usage["input_tokens"])
            if "output_tokens" in usage:
                output_tokens = int(usage["output_tokens"])

        result_text = result_event.get("result")
        if isinstance(result_text, str) and result_text:
            # Happy path: clean terminal text. Still bound it defensively.
            return ParsedOutput(
                stdout=_bound(result_text),
                session_id=session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=cost_usd,
            )

    # No clean result text — build a structured summary from the stream.
    summary = _summarise_events(events, result_event)
    return ParsedOutput(
        stdout=_bound(summary),
        session_id=session_id,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=cost_usd,
    )


def _summarise_events(events: list[dict], result_event: dict | None) -> str:
    """Build a structured summary string from the verbose event stream.

    Captures: tool-call counts by name, the final assistant text, and any
    ``errors`` array from the terminal result event.  The shape is
    deterministic so the audit log entries are scannable.
    """
    tool_counts: dict[str, int] = {}
    last_assistant_text = ""

    for event in events:
        if not isinstance(event, dict):
            continue
        if event.get("type") != "assistant":
            continue
        message = event.get("message") or {}
        content = message.get("content") or []
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use":
                name = str(block.get("name", "")).strip() or "unknown"
                tool_counts[name] = tool_counts.get(name, 0) + 1
            elif block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    last_assistant_text = text.strip()

    lines: list[str] = ["Agent run summary (no terminal result text)."]

    errors: list[str] = []
    if isinstance(result_event, dict):
        raw_errors = result_event.get("errors")
        if isinstance(raw_errors, list):
            errors = [str(e) for e in raw_errors if e]
    if errors:
        lines.append("")
        lines.append("Errors:")
        for err in errors:
            lines.append(f"  - {err}")

    if tool_counts:
        # Sort by descending count, then name, so the most-used tool is
        # first and the order is deterministic across runs.
        ordered = sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        rendered = ", ".join(f"{name} ×{count}" for name, count in ordered)
        lines.append("")
        lines.append(f"Tool calls: {rendered}")

    if last_assistant_text:
        lines.append("")
        lines.append("Last agent message:")
        lines.append(last_assistant_text)

    return "\n".join(lines)


def _bound(text: str) -> str:
    """Truncate *text* to :data:`_MAX_STDOUT_BYTES` UTF-8 bytes if needed.

    Uses head + ellipsis marker so the truncation is visible to anyone
    reading the audit log.  Truncating at byte boundaries is unsafe for
    multi-byte UTF-8; we operate on encoded bytes and decode with
    ``errors='ignore'`` to drop any partial code point at the boundary.
    """
    encoded = text.encode("utf-8")
    if len(encoded) <= _MAX_STDOUT_BYTES:
        return text
    marker = f"\n\n…[truncated: original {len(encoded)} bytes]"
    marker_bytes = marker.encode("utf-8")
    keep = _MAX_STDOUT_BYTES - len(marker_bytes)
    if keep < 0:
        # Pathological: cap smaller than the marker. Hard-cut.
        return encoded[:_MAX_STDOUT_BYTES].decode("utf-8", errors="ignore")
    head = encoded[:keep].decode("utf-8", errors="ignore")
    return head + marker
