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
# 1-1 portable evidence and offline seven-axis analysis specification

## Requirements

This is execution slice 1/3 of the accepted [master plan](../plans/session-health-jev.md), limited to T01–T04 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

This slice is ready for intake. It must produce a complete useful offline report without any model/key/network/SDK/agent CLI.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.1-pro-high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.


1. T01: Add Codex nested call/result and Copilot JSON-string arguments regression fixtures; preserve unknown/failed/unsupported records and reliable pairing.
2. T02: Build versioned portable SessionBundle, bounded canonical events/facts, source refs/capabilities, redacted evidence/case candidates and observation cutoffs; export/import round-trip.
3. T03: Implement all seven process-v2 observable axes with applicability/coverage/null denominators, transparent legacy score and optional identity-checked external outcome fixture joins.
4. T04: Integrate explicit offline mode across positional/single/batch CLI and terminal/JSON/HTML; keep every selected session status. Add corresponding documentation and CLI help.
5. Run offline parser, bundle, metrics and renderer unit/integration tests including no-network/no-model entry-point checks; collect RED/GREEN evidence.
6. AGY adversarial review of exact candidate, Luna repairs, then root independent verification. Document live/platform limits.

Maintain the eleven master requirements within this slice; defer unrelated later-stage implementation explicitly to its dependency successor, not as completed work.
