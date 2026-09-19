# Changelog

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
