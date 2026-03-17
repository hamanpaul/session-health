# Project docs

This directory holds the project-facing planning and research artifacts for the first refactor.

## Files

- `research/https-github-com-onestardao-wfgy-tree-main-problem.md` — imported research report on integrating WFGY ProblemMap / skill-problemmap into `session-health`.
- `plan.md` — synced mirror of the active implementation plan.
- `todo.md` — synced execution board derived from the approved plan and current SQL todo state.

## Current taxonomy decision

- The existing quantitative, PM1, and Atlas diagnostic categories are currently sufficient to express analysis results.
- The first refactor should only add report/document wrapper attributes when needed, rather than inventing new diagnostic families.

## Sync contract

- Session-state `plan.md` remains the plan-mode control file.
- Repo `docs/plan.md` and `docs/todo.md` are the formal project-facing documents and should stay in sync with session planning/tracking.
- Future implementation work should update both the SQL todo state and the repo docs mirror together.
