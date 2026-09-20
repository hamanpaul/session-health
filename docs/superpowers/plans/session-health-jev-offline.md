---
status: accepted
work_item: session-health-jev-offline
task_type: feature
issue: 5
domain_breadth: 1
state_consistency: 0
invariant_count: 11
artifact_classes: [source, tests, documentation]
---
# 1-1 portable evidence and offline seven-axis analysis

This is execution slice 1/3 of the accepted [master plan](session-health-jev.md), limited to T01–T04 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

This slice is ready for intake. It must produce a complete useful offline report without any model/key/network/SDK/agent CLI.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.1-pro-high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.

## Tasks

1. T01: Add Codex nested call/result and Copilot JSON-string arguments regression fixtures; preserve unknown/failed/unsupported records and reliable pairing.
2. T02: Build versioned portable SessionBundle, bounded canonical events/facts, source refs/capabilities, redacted evidence/case candidates and observation cutoffs; export/import round-trip.
3. T03: Implement all seven process-v2 observable axes with applicability/coverage/null denominators, transparent legacy score and optional identity-checked external outcome fixture joins.
4. T04: Integrate explicit offline mode across positional/single/batch CLI and terminal/JSON/HTML; keep every selected session status. Add corresponding documentation and CLI help.
5. Run offline parser, bundle, metrics and renderer unit/integration tests including no-network/no-model entry-point checks; collect RED/GREEN evidence.
6. AGY adversarial review of exact candidate, Luna repairs, then root independent verification. Document live/platform limits.

7. Update source changes, tests and documentation together, including CLI help and a changelog entry describing observable behavior and compatibility changes.

## Validation and scope

Source changes, tests and documentation are acceptance surfaces. Run meaningful unit/integration tests; update CLI help and changelog/release notes as applicable. Produce reports/verify and reports/review evidence. Do not tick master T10/T11 until all slices pass. Unknown usage remains null; mock API and portable fixtures are not live/API/platform proof.

Sizing: one bounded pipeline subsystem; immutable per-report transformations without external mutable state, no distributed transaction. Full feature breadth is handled by three sequential slices, not ignored.
