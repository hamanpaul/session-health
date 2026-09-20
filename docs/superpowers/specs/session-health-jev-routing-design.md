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
# Stage 2 local model discovery, Jev routing and checked diagnosis design

## Decisions

This is execution slice 3/3 of the accepted [master plan](../plans/session-health-jev.md), limited to T07–T09 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

Prerequisite: session-health-jev-semantic must be independently accepted and its exact candidate integrated into the canonical local base before intake. Do not dispatch concurrently against a stale base.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.8-flash-high with high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.


Use the master canonical data/report contracts and standard-library core. Keep the slice boundary T07–T09; see the [slice plan](../plans/session-health-jev-routing.md) for precise source, tests and documentation tasks. Compatibility and failure/unknown semantics are tested at actual CLI entry points.
