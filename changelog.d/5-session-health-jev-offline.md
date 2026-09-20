---
type: fix
scope: offline
issue: 5
---

Repair offline parser pairing, portable bundle validation, process-v2
semantics, and batch CLI status/exit reporting.

The raw JSONL reader now has independently configurable byte, record, and
record-size budgets with honest partial coverage. Portable replay retains
typed full-output SNR facts while keeping evidence text bounded, so direct and
export/import analysis agree.

Process-v2 now reports projected chronology as unknown, uses direct lifecycle
identity evidence consistently, and keeps default terminal/table/HTML output
free of legacy composite narratives. Bundle evidence can shrink to its byte or
event budget while retaining separate typed metric facts and explicit coverage.
