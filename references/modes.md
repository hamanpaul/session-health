# Session Health modes

| Environment | Command boundary | Stage-2 executor | Automatic fallback |
|---|---|---|---|
| Interactive | one waiting `--analysis-stdin` process | Current triggering agent | No |
| Explicit model | `--analyze --analysis-origin explicit-model --model MODEL` | Named concrete model | No by default |
| Headless | `--headless --analysis-origin headless --model-catalog-file FILE` | Multi-judge selection from active cards | At most one when explicitly enabled |
| Local only | `--offline` | None | No |

The trigger-agent stdin object accepts only these root fields:

```json
{
  "observations": [{"text": "...", "evidence_refs": [], "counterevidence_refs": []}],
  "hypotheses": [],
  "claims": [],
  "recommendations": [],
  "actual_model": null,
  "provider": null,
  "native_usage": {"input_tokens": null, "output_tokens": null, "total_tokens": null}
}
```

Each section permits at most 100 items. Each reference list permits at most 20
IDs and may contain only IDs present in the bounded context. Input is capped at
128000 bytes. Unknown fields, unknown references, invalid JSON, and empty input
are rejected without discarding the deterministic report.

Headless catalogs are operator evidence. Executable discovery alone reports
availability as unknown and cannot make a candidate runnable. Judges receive
only the task profile, candidate cards, and constraints; they do not run the
full session analysis. At most two eligible model judges plus one Jev vote are
collected under `headless-consensus-v1`.
