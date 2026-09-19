# Project docs

This directory holds the project's accepted plans, execution boards and research artifacts.

## Files

- `superpowers/plans/session-health-jev.md` — accepted implementation plan: 1-1 offline seven-axis analysis, 1-2 Jev batch enhancement, stage 2 dynamic model routing and checked diagnosis.
- `superpowers/specs/session-health-jev-spec.md` / `session-health-jev-design.md` — behavior and architecture contracts for the accepted plan.
- `superpowers/workstreams/session-health-jev/todo.md` — Cortex work source and task boundary.
- `superpowers/plans/session-health-jev-{offline,semantic,routing}.md` — sequential Cortex execution slices; matching stage workstreams own their slice todos.
- `jev-support-refinement.md` — superseded discussion draft; the accepted plan controls implementation.
- `research/https-github-com-onestardao-wfgy-tree-main-problem.md` — imported research report on integrating WFGY ProblemMap / skill-problemmap into `session-health`.
- `plan.md` — synced mirror of the active implementation plan.
- `todo.md` — synced execution board derived from the approved plan and current SQL todo state.

## Current taxonomy decision

- The seven axis IDs and PM1/Atlas taxonomy remain stable.
- The accepted Jev plan strengthens axis semantics in a versioned process-v2 profile, preserving the documented legacy profile.

## Sync contract

- The accepted plan/spec/design define current requirements; `docs/plan.md` links the current plan and retains historical decisions.
- The workstream todo is the single Cortex active todo source; `docs/todo.md` is its readable execution-board mirror.
- The operator keeps a local SQLite task ledger alongside run evidence; it is separate from Cortex's Manager-owned lifecycle registry.
