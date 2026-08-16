# {title}

## Spec (context only — the conflict markers below are ground truth)

{artifact:draft_spec}

## Instructions

The `## Revision Feedback` section below lists either:

- **First dispatch**: the conflicted files that resulted from the orchestrator
  merging `origin/main` into this branch. Resolve all conflict markers in
  those files, then emit `AGENT_RESULT: COMPLETED:`.
- **Subsequent dispatches** (after `NEEDS_INPUT:` escalation): the operator's
  answer to your question. Apply the decision, finish resolving any remaining
  markers, and emit `AGENT_RESULT: COMPLETED:`.
