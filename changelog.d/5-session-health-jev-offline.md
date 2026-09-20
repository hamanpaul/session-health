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

Raw-input limit diagnostics now distinguish a retained usable prefix
(`partial`) from an input with no usable records (`failed`), and parser
diagnostics keep numeric line and source-reference fields consistent.

Process-v2 now reports projected chronology as unknown, uses direct lifecycle
identity evidence consistently, and keeps default terminal/table/HTML output
free of legacy composite narratives. Bundle evidence can shrink to its byte or
event budget while retaining separate typed metric facts and explicit coverage.
Coverage now distinguishes original input completeness, replayability of the
observed typed facts, and evidence-excerpt truncation; raw read limits produce
honest partial/failed processing status while an evidence-only cap does not
discard full SNR, STATE, lifecycle, or tool-identity facts. Portable imports
reject POSIX and Windows absolute source references, and STATE treats explicit
per-turn absence, inherited context, and metadata-only fields distinctly.
Redacted command arguments retain one-way executable identity facts, so
bounded replay does not turn unrelated absolute-path commands into retries.
