---
name: session-health
description: Analyze local Codex or Copilot sessions with deterministic seven-axis process-v2 metrics, optional Jev semantic refinement, and a bounded stage-2 synthesis. Use for session health, seven-axis reports, turn_aborted, context_compacted, tool-efficiency, session comparison, or HTML radar reports. In interactive use, keep stage 2 on the current triggering agent unless the user explicitly names another model; use multi-model selection only for explicit headless runs.
---

# Session Health

Use the Python engine in this skill directory for parsing, seven-axis facts,
Jev transport, validation, and rendering. Keep the current interactive agent as
the default stage-2 analyzer.

## Choose the mode

- Interactive, no user model override: use the two-step trigger-agent protocol below. Do not invoke Codex, Copilot, agy, or another model adapter.
- User names a model: use `--analyze --analysis-origin explicit-model --model MODEL`. Do not change models after a failure unless the user explicitly allows fallback.
- Headless: use `--headless --analysis-origin headless` with an operator-confirmed model catalog. This is the only mode that discovers active candidates and asks bounded model-selection judges.
- Basic/local only: use `--offline`. It always retains the deterministic report and never calls Jev or an analyzer.

`--jev` adds typed semantic refinement and an optional post-check. A missing key,
empty response, abstention, or transport failure never changes the interactive
stage-2 executor. Check only whether `TYPESAFE_API_KEY` exists; never print it.

## Interactive no-file protocol

Set `ENGINE` to `eval_session.py` beside this file. Do not create
`analysis-input.json` or `analysis-result.json`.

1. Start one report process:

   `python3 "$ENGINE" SESSION --analysis-stdin --analysis-origin trigger-agent --format html --output REPORT.html [--jev]`

2. Read the `SESSION_HEALTH_ANALYSIS_CONTEXT` JSON emitted on stderr while the
   process waits for stdin. Analyze it in the current agent context. Return one
   JSON object with arrays named `observations`, `hypotheses`, `claims`, and
   `recommendations`. Every item has `text`, `evidence_refs`, and
   `counterevidence_refs`. Include `actual_model`, `provider`, and provider-owned
   `native_usage` only when known. Never include hidden reasoning.

3. Send that JSON to the same process through stdin, followed by EOF. Use the
   agent runtime's stdin/PTY facility so the JSON is data, not shell code.

`--analysis-context` is a read-only preview that exits after printing the same
bounded context. The standard interactive flow uses the single waiting process,
so parsing and optional Jev refinement run once.

## Output and failure rules

- Preserve `process-v2` results when Jev or stage 2 fails.
- Keep unknown, missing, deferred, abstained, and failed states distinct.
- A trigger-agent validation failure sets `trigger_agent_analysis_failed` and leaves the basic report usable.
- Report `analysis_origin`, requested and actual model, routing mode, fallback policy, judge receipts, and native usage. Keep unavailable values null.
- For a sessions directory, use the batch report and its average seven-axis radar. Per-session evidence remains available below it.

See [references/modes.md](references/modes.md) for the command matrix and schema.
