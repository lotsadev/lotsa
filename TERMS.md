# Terms of Use

**Effective date:** 2026-07-13 · **Last updated:** 2026-07-13

These terms govern your use of Lotsa, open-source software available at
[github.com/lotsadev/lotsa](https://github.com/lotsadev/lotsa) (the
"Software"). By running or using the Software, you agree to them. If you
don't agree, don't use it.

The Software is authored and maintained by Andrew Crookston. It is
licensed to you under the [Apache License 2.0](LICENSE); these terms are
additional usage terms about running an AI agent against your own code —
they don't replace or narrow the license.

## 1. What Lotsa is

Lotsa is a local task runner and dashboard that dispatches an AI coding
agent (Claude Code, or another provider you configure) against git
repositories you register, following a process you define. It runs
entirely on your own machine or infrastructure. There is no hosted
service, no account, and no central server operated by the author.

## 2. Acceptable use

Use Lotsa only for lawful purposes, and only against repositories you own
or have permission to modify. You must not use it to create or distribute
illegal content, or to attempt to disrupt or gain unauthorized access to
systems you don't control.

## 3. Your code and content

Your code, tasks, and everything Lotsa produces while working on them
remain entirely yours. Neither the Software nor its author claims any
rights in your code, collects it, or uses it for any purpose other than
running the task you gave it, on your own machine.

## 4. Agent access and risk — read this before pointing Lotsa at a repo you care about

Running Lotsa means granting an AI coding agent read/write access to the
repositories you register. Within its worktree, the agent can:

- read, create, edit, and delete files;
- commit changes;
- push branches and open or update pull requests (when GitHub push/PR
  features are enabled).

This is the core of what Lotsa does, and it carries real risk. A bug in
Lotsa, a mistake by the underlying model, a misconfigured process, or
simple misuse can result in **unwanted file changes, lost or overwritten
work, unintended commits, or unintended pull requests**. Lotsa's design
(isolated worktrees, orchestrator-owned git state, PR-based review)
reduces this risk but cannot eliminate it.

**You are responsible for:**

- reviewing agent output and PRs before merging;
- keeping your own backups and using version control as a safety net;
- never pointing Lotsa at a repository, branch, or credential you can't
  afford to have modified;
- understanding that a task, once dispatched, can commit and push before
  you've reviewed every line.

## 5. Costs

Running Lotsa dispatches calls to whichever LLM provider (and, optionally,
GitHub API) you've configured. **You are solely responsible for any usage
costs or charges those services bill you.** Lotsa's `--budget` setting is
a soft cap enforced on a best-effort basis by the runner in use — it is
not a guarantee against unexpected charges, and not every runner shape
enforces it (see the README's Agent runners section).

## 6. No warranty

The Software is provided **"as is"**, without warranty of any kind,
express or implied, to the fullest extent permitted by law — echoing (and
not replacing) the warranty disclaimer already in the Apache License 2.0,
§7. Lotsa does not warrant that it will be error-free, secure, or fit for
any particular purpose.

## 7. Limitation of liability

To the fullest extent the law allows, neither the Software's author nor
its contributors are liable for any loss or damage arising from your use
of it — including lost, corrupted, or unintended changes to your code,
unintended commits or pull requests, or costs incurred from services you
configured. This is free software; to the extent liability cannot be
excluded, it is limited to zero. This section supplements, and does not
narrow, the liability limitation already in the Apache License 2.0, §8.

## 8. Changes

These terms may be updated from time to time. The current version always
lives at [`TERMS.md`](https://github.com/lotsadev/lotsa/blob/main/TERMS.md)
in the repository, with the date above. Continued use after a change means
you accept it.

## 9. Governing law

These terms are governed by the laws of Sweden. Any dispute arising from
them is subject to the exclusive jurisdiction of the Swedish courts, to
the extent mandatory consumer-protection law in your own country of
residence doesn't provide otherwise.

## 10. Contact

Questions about these terms: open an issue at
[github.com/lotsadev/lotsa/issues](https://github.com/lotsadev/lotsa/issues).
