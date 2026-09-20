# Jev semantic enhancement

The CLI now accepts explicit `--jev` to add an optional, bounded semantic
judgment layer. The standard-library backend validates Choice/Noul/Score
answers, records request/attempt provenance and usage without per-case double
counting, and classifies retryable, failed, deferred, and unknown outcomes.

Seven versioned axis question groups use one redacted shared state and bounded
multi-case batches. Semantic results are additive in JSON, terminal, and HTML
reports; deterministic offline facts and legacy compatibility fields remain
available. A missing `TYPESAFE_API_KEY` is reported as live deferred, and
synthetic mock validation is not presented as live/API or human-label proof.
