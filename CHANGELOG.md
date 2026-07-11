# Changelog

All notable changes to Lotsa are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and Lotsa uses
[semantic versioning](https://semver.org/) — while pre-1.0, breaking changes
bump the **minor** version.

## [0.2.0] — 2026-07-09

Breaking-change release. The task catalog, guard overrides, and restart
behaviour changed shape since 0.1.0; review the items below before upgrading a
running deployment.

> **Before you upgrade: complete any ongoing tasks first.** Existing task rows
> are preserved across the upgrade, but a task created under the old catalog
> won't show its workflow/steps under the new two-phase model — so finish your
> in-flight tasks before you upgrade.

### Breaking

- **Two-phase Think→Execute catalog** ([#11]) — the five-preset catalog is
  replaced by a two-phase Think→Execute model. Process/flow names and the
  default new-task flow changed; custom `process.yaml`s written against the old
  preset names need updating.
- **Guard overrides drop the operator-reason field** ([#12]) — acknowledging a
  guard override no longer takes (or records) a free-text reason. Callers/UI
  that supplied one must stop sending it.
- **Restart is resumptive, not destructive** ([#8], ADR-040) — on restart the
  orchestrator resumes interrupted (`status='working'`) tasks instead of
  discarding them. Operators relying on the old "restart clears in-flight work"
  behaviour will see tasks resume instead.

### Added

- `lotsa deploy` — single-host deploy CLI with no repo checkout required ([#3],
  ADR-042).
- Task-prompt attachments: operators can attach files to a task ([#13]), now
  rendered inline in the chat and the right panel ([#25]).
- Changes tab explains a missing diff for terminal tasks and links out to the
  PR ([#22]).
- Mobile-first dashboard below 768px ([#6]); new-task empty-state redesign
  ([#2]); Lotsa tug favicon + logo ([#10]).

### Fixed

- A publish reconcile-conflict now dispatches `resolve_conflicts` instead of
  dead-ending at `blocked` ([#16]).
- A chat message to an error-blocked task no longer strands it at
  `(status=working, state=blocked)`; `stop()` can clear such a torn row ([#18]).
- Dashboard: refetch on window focus so returning to the tab shows the current
  gate ([#7]); chat overflow/dedupe and distinct "You" styling ([#9]); vertical
  rhythm and soft line breaks in chat messages ([#14]); restored code-block
  rendering in chat ([#24]); a pr-fix skip no longer duplicates its reasoning or
  overflows ([#26]); the Promote popup is wide enough ([#23]).
- `make deploy` now runs `make build` first so a stale wheel can't ship ([#21]).
- CI: Claude PR review no longer produces empty output ([#4]); CI fires on
  `lotsa/*` branches with the missing checks added ([#19]).

## [0.1.0] — 2026-06-28

Initial public release: local task runner, web dashboard, and CLI for Claude
Code, published to PyPI (`pip install lotsa`).

[0.2.0]: https://github.com/lotsadev/lotsa/releases/tag/v0.2.0
[0.1.0]: https://github.com/lotsadev/lotsa/releases/tag/v0.1.0
[#2]: https://github.com/lotsadev/lotsa/pull/2
[#3]: https://github.com/lotsadev/lotsa/pull/3
[#4]: https://github.com/lotsadev/lotsa/pull/4
[#6]: https://github.com/lotsadev/lotsa/pull/6
[#7]: https://github.com/lotsadev/lotsa/pull/7
[#8]: https://github.com/lotsadev/lotsa/pull/8
[#9]: https://github.com/lotsadev/lotsa/pull/9
[#10]: https://github.com/lotsadev/lotsa/pull/10
[#11]: https://github.com/lotsadev/lotsa/pull/11
[#12]: https://github.com/lotsadev/lotsa/pull/12
[#13]: https://github.com/lotsadev/lotsa/pull/13
[#14]: https://github.com/lotsadev/lotsa/pull/14
[#16]: https://github.com/lotsadev/lotsa/pull/16
[#18]: https://github.com/lotsadev/lotsa/pull/18
[#19]: https://github.com/lotsadev/lotsa/pull/19
[#21]: https://github.com/lotsadev/lotsa/pull/21
[#22]: https://github.com/lotsadev/lotsa/pull/22
[#23]: https://github.com/lotsadev/lotsa/pull/23
[#24]: https://github.com/lotsadev/lotsa/pull/24
[#25]: https://github.com/lotsadev/lotsa/pull/25
[#26]: https://github.com/lotsadev/lotsa/pull/26
