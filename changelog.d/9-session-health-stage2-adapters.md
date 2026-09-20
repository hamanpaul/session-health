# Stage-2 report-only adapter readiness

Built-in and operator-configured Codex analysis now uses the installed
report-only contract: stdin prompt delivery, read-only sandbox, never-approval,
repo-check bypass, and native JSONL events. The adapter extracts the final
assistant JSON/text and provider-reported usage without estimating omitted
fields. AGY print-mode adapters now explicitly use plan mode and sandbox while
preserving the supported `--print <prompt>` argv transport.
