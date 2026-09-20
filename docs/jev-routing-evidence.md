# Evidence-based Jev routing

A session-health routing incident returned `no_suitable_model` despite two
operator-admitted candidates. Reconstructing its original wire payload matched
the recorded request hash. The state contained only a generic purpose, byte
limits and routing cards, with no task description or quality evidence; all
Choice criteria were null.

A bounded 15-request experiment using provider-confirmed `jev-1.13.0` found:

| Probe | Result |
|---|---|
| Original structure, twice | Both abstained |
| Add only a concrete task profile | Selected a candidate |
| Add task profile and defined provisional-selection criteria, twice | Both selected a candidate |
| Add four discovered but unverified candidates | Selected an operator-admitted candidate; unverified options received zero Choice probability |
| Fictional capability cards, anonymized cards, swapped evidence | Followed the supplied capability evidence in all three controls |
| Model names only, including a fictional name | Returned insufficient evidence |
| All candidates unavailable | Returned no suitable model |
| Two equally suitable candidates | Selected one with high confidence |

This is one incident and small controlled probes, not a calibrated benchmark.
The original served model was not retained, so replay does not establish the
historical version. The criteria intervention included policy clarification,
not only formatting. Near-equal candidates changed ordering after expanding
the candidate list. Selection success does not establish downstream quality.
The original experiment used 24,558 input and 1,815 output tokens across 27
questions, with no second-stage analyzer invocation.

## Contract

1. Python discovers candidates and checks executable/route availability,
   freshness, byte limits and explicit constraints. Discovery alone does not
   establish account access. The optional local Codex cache is an advertised
   inventory, with hidden entries omitted and missing/invalid files tolerated.
2. `AnalysisRequest.task_profile` carries a bounded, report-safe description of
   the requested work. CLI callers supply single/batch scope and session count.
   It includes seven-axis interpretation, evidence references, unknowns, and
   concise Traditional Chinese observations/hypotheses/recommendations. Raw
   session transcripts are not copied into routing context.
3. `AgentConfig.capability_evidence` carries operator-supplied descriptions and
   scoped observations, with provenance. Cache advertisements remain distinct
   from measured task quality; model names alone are not capability proof.
4. Jev Choice receives explicit option definitions and a provisional-selection
   objective. Missing essential evidence and known unsuitability are separate
   abstention reasons. Multiple usable alternatives alone do not require an
   abstention. Python resolves exact probability ties deterministically.
5. Reports retain the native distribution, confidence, requested/served model,
   request/response hashes and native usage. These fields explain the decision
   evidence without inventing freeform reasoning or calibrated quality scores.

The seven axes, offline-only path and existing executor adapters remain in
place. An explicit Jev abstention still produces a partial second-stage result;
there is no new policy to force a candidate despite missing essential facts.
Real task-matched quality comparisons can later enrich capability cards from
normal execution receipts without changing the provider contract.

Jev evaluates independent questions against one shared state, so multiple
candidate-fit questions can be batched if needed; answers do not depend on each
other within that request. The production routing path keeps one Choice request
and does not add diagnostic calls to every analysis. Official documentation:
[Choice](https://docs.typesafe.ai/primitives/choice),
[API response fields](https://docs.typesafe.ai/api),
[shared state and context limits](https://docs.typesafe.ai/models),
[known limits including irrelevant context and confidence invariants](https://docs.typesafe.ai/model-jaggedness/jev-1.13).
