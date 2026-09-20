# Changelog

## 2026-09-20

- Added bounded stage-2 analyzer catalog/routing for concrete Codex, Copilot,
  and agy executor/route/model/settings cards. Read-only CLI discovery now
  preserves unknown account availability, while explicit overrides and
  operator entries remain distinguishable; analyzer prompts use stdin and
  retain requested versus actual identity plus native usage.
- Added deterministic fallback, one bounded failed-execution reselection, and
  evidence-frozen Jev post-checks that preserve contradictions/abstentions and
  allow at most one repair round. Routing and post-check metadata are additive
  in JSON, terminal, and HTML reports.
- Repaired routing pilot aggregate-budget accounting and invalid-case reporting,
  isolated failed analyzer catalog entries per invocation, and aligned the agy
  adapter with the installed `--print <prompt>` argv contract while retaining
  the native JSON response/usage capture.
- Added explicit `--jev` semantic reporting with bounded typed Choice/Noul/Score
  batches, seven versioned axis question groups, shared redacted state,
  request-level provenance/usage accounting, and deferred status when live Jev
  credentials are unavailable.

## 2026-09-19

- Added nested Codex call/result and Copilot JSON-string argument pairing with
  explicit success/failed/unknown diagnostics.
- Added bounded, versioned portable `SessionBundle` export/import with relative
  source refs, redacted evidence and observation cutoffs.
- Added the no-LLM `process-v2` observable seven-axis report and explicit
  `--offline` CLI mode, including per-session batch failure status and optional
  identity-checked outcome fixtures.
- Preserved the legacy composite fields and added CLI/documentation coverage for
  the compatibility profile.
- Kept process-v2 renderers free of uncalibrated legacy aggregates, made projected
  chronology explicit, and bounded canonical bundle projections while preserving
  separate typed facts for replay.
