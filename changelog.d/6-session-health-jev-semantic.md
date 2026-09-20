# Jev semantic enhancement

The CLI now accepts explicit `--jev` to add an optional, bounded semantic
judgment layer. The standard-library adapter uses the official Jev wire
contract (`state`, `model`, and an ID-keyed `questions` map with lowercase
primitive types), validates Choice/Noul/Score answers, retains native
distributions/legends/confidence, and records invocation-level request/attempt
provenance and usage without per-case double counting. Long `Retry-After`
values defer instead of being silently shortened, and invalid responses retain
an unknown wire attempt.

Seven versioned axis question groups use one redacted shared state and bounded
multi-case batches. Dependent stages receive retained earlier judgments and
case paths address the keyed state projection. Semantic results disclose source
versus selected cases and sampling coverage, while remaining additive in JSON,
terminal, and HTML reports; deterministic offline facts and legacy
compatibility fields remain available. A missing `TYPESAFE_API_KEY` is reported
as live deferred, and synthetic mock validation is not presented as live/API or
human-label proof.

Pre-archive repair now keeps native Score answers whose weighted value differs
within a bound derived from the response's visible decimal precision, retaining
the reported score/distribution and a consistency diagnostic while still
rejecting gross contradictions. Reused backends expose per-session ledger and
usage snapshots; aggregate request, attempt, and byte budgets remain bounded at
backend-instance scope. Offline parser diagnostic provenance is also aligned
with the accepted prerequisite repair.
