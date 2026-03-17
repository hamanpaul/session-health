# Todo

> Synced execution board generated from the approved session plan and current SQL todo state.
> Docs setup, report-bundle / ProblemMap refactor, and weighted diagnosis integration are complete.

## Execution rule

1. Keep `docs/todo.md` readable for humans.
2. Keep SQL as the queryable state source while this file mirrors the current execution board.
3. When new work starts, update SQL first and mirror the change here.

## Current status

- No active refactor todos remain in SQL.
- The current milestones now ship a unified report pipeline with `SessionReport` / `BatchReport`, embedded ProblemMap diagnosis, bundle-aware terminal / JSON / HTML rendering, a one-command positional bundle flow for `Session ID` / `session path`, Chinese ProblemMap copy, a weighted diagnosis layer that exposes `PM1 / PM2 / PM3` meanings plus `Fx` weighting ratios, and a Codex main-agent default upgraded to `gpt-5.4`.

## Done

- `confirm-doc-sync-mode` — Decide whether repo docs/plan.md and docs/todo.md are continuously synchronized living documents or one-time snapshots derived from session artifacts.
- `define-wrapper-attributes` — Confirm that PM1/Atlas/7-axis taxonomies stay unchanged and only add report/document wrapper attributes such as target_kind, report_kind, evidence_summary, artifact_sources, and sync_status.
- `design-docs-structure` — Define the repository docs/ layout, file names, and source-of-truth rules for research, plan, and todo artifacts after docs/todo.md generation.
- `generate-docs-todo` — Generate repo docs/todo.md from the approved session plan.md and SQL todo state, then use it as the execution board for the first refactor.
- `introduce-report-types` — Add structured report wrapper dataclasses for single-session and batch reports without changing PM1, Atlas, or 7-axis diagnostic taxonomies.
- `wire-report-types-through-pipeline` — Use the new report wrapper types in the analysis pipeline so quantitative, ProblemMap, agent, and artifact metadata layers can be carried together.
- `integrate-problemmap-layer` — Embed ProblemMap / Atlas diagnosis into the core session-health pipeline rather than treating it as a separate mode.
- `expand-renderers-for-report-bundle` — Refactor terminal and HTML renderers to consume report wrappers and show quantitative, ProblemMap, and agent sections coherently.
- `link-docs-entrypoints` — Decide whether to add docs/README.md and/or README links so the new docs/ area is discoverable.
- `plan-artifact-flow` — Specify how research markdown, session plan.md, and SQL todo state map into repo docs/ files, with docs/todo.md generated first and used as the execution board.
- `validate-doc-consistency` — Validate that repo docs and session artifacts remain aligned after implementation, especially continuous synchronization of docs/plan.md and docs/todo.md.
- `localize-problemmap-copy` — Add Chinese display fields and render ProblemMap / Atlas sections in Chinese without breaking the underlying English diagnostic structure.
- `design-diagnosis-summary-schema` — Define the weighted diagnosis summary model that keeps quantitative and ProblemMap separate internally, adds Chinese explanations for PM1/PM2/... fields, and exposes explicit Fx (F1~F7) weighting metadata.
- `build-diagnosis-summary-pipeline` — Implement the builder that maps SessionScore, evidence_summary, and ProblemMapDiagnosis into a weighted diagnosis layer showing ProblemMap -> Fx weights -> quantitative interpretation uplift.
- `unify-diagnosis-renderers` — Replace separate ProblemMap and Evidence Summary sections with one weighted diagnosis section that still shows source layering plus PM field meanings and Fx weighting ratios.
- `align-json-and-agent-diagnosis` — Update JSON output and agent prompts to include raw scores, raw ProblemMap, PM field meanings, Fx weights, and the weighted diagnosis summary.
- `validate-diagnosis-integration` — Run compile/help/current-session/synthetic validation to confirm the weighted diagnosis flow and PM/Fx explanations render correctly for single-session and batch outputs.
- `sync-docs-diagnosis-integration` — Refresh docs/todo.md and any directly affected docs after the weighted diagnosis integration is implemented and validated.
- `switch-codex-main-model` — Change the main Codex agent model reference from `gpt-5.3` to `gpt-5.4`, validate the CLI flow, and prepare the work on a new branch.

## Current attribute taxonomy decision

- Keep the existing quantitative (`SNR/STATE/CTX/REACT/DEPTH/CONV/TOOL`), PM1, and Atlas diagnostic categories unchanged for the first refactor.
- Add only report/document wrapper attributes first: `target_kind`, `report_kind`, `analysis_layers`, `evidence_summary`, `artifact_sources`, and `sync_status`.
