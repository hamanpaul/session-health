---
status: accepted
work_item: session-health-jev-semantic
task_type: feature
issue: 6
domain_breadth: 1
state_consistency: 1
invariant_count: 11
artifact_classes: [source, tests, documentation]
---
# 1-2 Jev typed seven-axis batch enhancement specification

## Requirements

This is execution slice 2/3 of the accepted [master plan](../plans/session-health-jev.md), limited to T05–T06 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

Prerequisite: session-health-jev-offline must be independently accepted and its exact candidate integrated into the canonical local base before intake. Do not dispatch concurrently against a stale base.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.1-pro-high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.


1. Implement a capability-aware generic semantic backend and standard-library Jev HTTP adapter with Choice/Noul/Score schema/range validation.
2. Add bounded request/state/question/attempt/time budgets, conservative byte limits, retry classification, provenance and correct request-level usage accounting.
3. Implement all seven versioned semantic question groups, applicability/abstention, independent questions sharing state across multiple cases, and bounded dependent stages.
4. Integrate --jev into CLI and all report surfaces while preserving offline facts, raw judgments, coverage, partial failure and no automatic generative analysis.
5. Run synthetic mock E2E and labeled English/Traditional-Chinese pilot fixtures; perform a small non-sensitive live Jev smoke only if the key is visible and record actual model/tokens.
6. AGY adversarial review of exact candidate, Luna repairs, then root independent verification.

Maintain the eleven master requirements within this slice; defer unrelated later-stage implementation explicitly to its dependency successor, not as completed work.
