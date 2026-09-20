# Stage-2 integration wiring repair

The analyzer now receives bounded portable-bundle facts, process-v2 observations,
and adopted semantic judgments for both single-session and batch prompts while
keeping their coverage, cutoffs, evidence references, and unknowns separate.
Native analyzer envelopes and Markdown observations, hypotheses, claims, and
recommendations are parsed into the Jev post-check; non-empty unparseable
output is partial, and one same-card repair round uses the frozen evidence and
shared request budget.

The CLI accepts an explicit JSON operator catalog through
`--model-catalog-file`, including concrete Codex/Copilot/AGY model and effort
settings. Read-only executable discovery remains unknown access, and built-in
adapters do not accept shell templates or global tool/write flags.
