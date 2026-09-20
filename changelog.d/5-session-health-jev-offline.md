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
