# Jev routing and checked diagnosis

The second-stage analyzer now uses a concrete executor/provider/route/model/
settings catalog for Codex, Copilot, and agy. Read-only executable discovery
records `unknown` account availability instead of treating an installed CLI as
proof of access. Explicit operator entries and `--model` overrides are kept
separate from discovery provenance, and routing applies bounded context,
output, latency, capability, and cost constraints.

Analyzer prompts are sent through bounded argv/stdin adapters. Reports retain
requested and provider-reported actual identity/settings independently; absent
native usage remains null. A Jev Choice can select a concrete candidate, with a
versioned deterministic fallback and at most one failed-execution reselection.

When `--jev` and `--analyze` are combined, structured claims and
recommendations are checked in one batch against a frozen original evidence
snapshot. Supported, contradicted, overclaimed, and insufficient results remain
visible, and only one repair round is allowed. Existing offline facts and
legacy report fields remain additive and compatible.

The routing-vs-baseline synthetic pilot now uses bounded aggregate request
budgets across all cases. Budget failures and Jev abstentions remain partial
and are not counted as baseline agreement; a missing semantic backend is
reported as not applicable. Analyzer catalog entries are cloned per invocation
so execution failures cannot mutate the shared catalog. The agy adapter uses
the installed `--print -` stdin contract and JSON output for native usage when
the provider reports it.

Synthetic routing/post-check fixtures are not live executor, API, or human-label
proof; live availability and quality remain explicitly provisional.
