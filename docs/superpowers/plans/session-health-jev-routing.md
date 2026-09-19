---
status: accepted
work_item: session-health-jev-routing
task_type: feature
issue: 7
domain_breadth: 1
state_consistency: 1
invariant_count: 11
artifact_classes: [source, tests, documentation]
---
# Stage 2 local model discovery, Jev routing and checked diagnosis

This is execution slice 3/3 of the accepted [master plan](session-health-jev.md), limited to T07–T09 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

Prerequisite: session-health-jev-semantic must be independently accepted and its exact candidate integrated into the canonical local base before intake. Do not dispatch concurrently against a stale base.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.1-pro-high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.

## Tasks

1. Add concrete executor/route/model/settings catalog with read-only Codex/Copilot/agy discovery, operator entries, availability provenance and freshness; do not infer account access from installed CLI.
2. Implement Python hard eligibility/budget constraints and Jev Choice selecting concrete candidates, explicit override, deterministic no-Jev fallback and bounded failed-execution reselection.
3. Run the chosen analyzer through bounded argv/stdin adapters; record requested/actual model/settings and native usage without fabricating unknown tokens.
4. Batch-check generated claims/recommendations against frozen original evidence with Jev; preserve contradictions, abstentions and at most one repair.
5. Complete whole-batch status and renderer/CLI integration, legacy regression, synthetic E2E, routing-vs-baseline pilot and documentation. Keep all-selected coverage, identity and usage stage separation.
6. AGY adversarial review of exact candidate, Luna repairs, then root final integrated verification across 1-1, 1-2 and stage 2.

## Validation and scope

Source changes, tests and documentation are acceptance surfaces. Run meaningful unit/integration tests; update CLI help and changelog/release notes as applicable. Produce reports/verify and reports/review evidence. Do not tick master T10/T11 until all slices pass. Unknown usage remains null; mock API and portable fixtures are not live/API/platform proof.

Sizing: one bounded pipeline subsystem; bounded request/availability/usage state inside one local pipeline, no distributed transaction. Full feature breadth is handled by three sequential slices, not ignored.
