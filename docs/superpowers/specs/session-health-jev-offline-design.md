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
# 1-1 portable evidence and offline seven-axis analysis design

## Decisions

This is execution slice 1/3 of the accepted [master plan](../plans/session-health-jev.md), limited to T01–T04 plus its own tests, documentation, adversarial review and root verification. The master plan controls semantic/API contracts; later stages remain required in their own slices.

This slice is ready for intake. It must produce a complete useful offline report without any model/key/network/SDK/agent CLI.

Builder: codex/gpt-5.6-luna with max; reviewer: agy/gemini-3.1-pro-high, read-only exact candidate. One builder writer; no other repository, registry, credentials, push/PR/merge/deploy mutations by builder. Follow master plan section 9. Unhandled defects/gaps fail review; documented bounded residual risk alone does not, unless reviewer rebuts its impact analysis. Findings: at most 10 BLOCKER/MAJOR with reproducible triggers, file locations and PASS/FAIL.


Use the master canonical data/report contracts and standard-library core. Keep the slice boundary T01–T04; see the [slice plan](../plans/session-health-jev-offline.md) for precise source, tests and documentation tasks. Compatibility and failure/unknown semantics are tested at actual CLI entry points.
