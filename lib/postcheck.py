"""Evidence-frozen Jev post-checks for generated analysis.

This module treats generated text as a hypothesis layer.  The original bundle
is copied and hashed before any claim is checked, so a generated response
cannot silently become evidence.  Contradictions and abstentions are retained
as first-class results, and repair is deliberately limited to one round.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .semantic_backend import (
    SemanticBudget,
    SemanticLedger,
    SemanticQuestion,
    SemanticState,
    SemanticUsage,
    UnavailableSemanticBackend,
    stable_hash,
)


POSTCHECK_VERSION = "postcheck-v1"
CHECK_CHOICES = ("supported", "contradicted", "overclaimed", "insufficient")


def _bounded(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            key_text = str(key)
            if any(marker in key_text.lower() for marker in ("key", "token", "password", "secret", "authorization", "cookie")):
                output[key_text] = "[redacted]"
            else:
                output[key_text] = _bounded(item, depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, str):
        return value[:12_000]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:2_000]


def freeze_evidence(evidence: Any) -> Dict[str, Any]:
    """Create a bounded, JSON-safe evidence snapshot and never mutate input."""

    if hasattr(evidence, "facts") or hasattr(evidence, "evidence_refs"):
        payload = {
            "facts": getattr(evidence, "facts", {}),
            "evidence_refs": getattr(evidence, "evidence_refs", []),
            "cases": getattr(evidence, "cases", []),
            "coverage": getattr(evidence, "coverage", {}),
        }
        manifest = getattr(evidence, "manifest", {})
        if isinstance(manifest, Mapping):
            payload["bundle_identity"] = {
                key: manifest.get(key)
                for key in ("schema", "version", "artifact_id", "session_id", "source_ref")
                if manifest.get(key) is not None
            }
    elif hasattr(evidence, "turns") and hasattr(evidence, "source"):
        payload = {
            "session": {
                "source": getattr(evidence, "source", "unknown"),
                "model": getattr(evidence, "model", None),
                "turn_count": len(getattr(evidence, "turns", []) or []),
            },
            "turns": [
                {
                    "index": getattr(turn, "index", None),
                    "user_input": getattr(turn, "user_input", "")[:4_000],
                    "assistant_output": getattr(turn, "assistant_output", "")[:4_000],
                    "tool_calls": [
                        {
                            "name": getattr(call, "name", ""),
                            "output": getattr(call, "output", "")[:4_000],
                            "success": getattr(call, "success", None),
                            "exit_code": getattr(call, "exit_code", None),
                        }
                        for call in (getattr(turn, "tool_calls", []) or [])[:20]
                    ],
                }
                for turn in (getattr(evidence, "turns", []) or [])[:100]
            ],
        }
    elif isinstance(evidence, Mapping):
        payload = dict(evidence)
    elif isinstance(evidence, (list, tuple)):
        payload = {"items": list(evidence)}
    else:
        payload = {"value": evidence}

    safe = _bounded(copy.deepcopy(payload))
    if not isinstance(safe, Mapping):
        safe = {"value": safe}
    # Deep copy once more so a caller retaining references cannot modify the
    # object held by the post-check result.
    return json.loads(json.dumps(dict(safe), ensure_ascii=False, sort_keys=True, allow_nan=False))


def evidence_hash(evidence: Mapping[str, Any]) -> str:
    return stable_hash(evidence)


def normalize_claims(
    claims: Optional[Iterable[Any]] = None,
    recommendations: Optional[Iterable[Any]] = None,
) -> List[Dict[str, Any]]:
    """Normalize generated claims and recommendations without changing text."""

    result: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for kind, items in (("claim", claims or ()), ("recommendation", recommendations or ())):
        for index, raw in enumerate(items):
            if isinstance(raw, Mapping):
                text = raw.get("text", raw.get("claim", raw.get("recommendation", "")))
                item = dict(raw)
            else:
                text = raw
                item = {}
            if not isinstance(text, str) or not text.strip():
                continue
            text = text.strip()[:12_000]
            item["text"] = text
            item.setdefault("kind", kind)
            item.setdefault("claim_id", f"{kind}-{index + 1}")
            claim_id = str(item["claim_id"])
            if claim_id in seen:
                claim_id = f"{claim_id}-{len(result) + 1}"
                item["claim_id"] = claim_id
            seen.add(claim_id)
            refs = item.get("evidence_refs", [])
            item["evidence_refs"] = [str(ref) for ref in refs[:20]] if isinstance(refs, list) else []
            result.append(item)
    return result


def extract_generated_claims(output: Any) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Extract explicitly labelled claims/recommendations from structured or text output."""

    if isinstance(output, Mapping):
        return (
            list(output.get("claims", [])) if isinstance(output.get("claims", []), list) else [],
            list(output.get("recommendations", [])) if isinstance(output.get("recommendations", []), list) else [],
        )
    text = str(output or "")
    claims: List[Dict[str, Any]] = []
    recommendations: List[Dict[str, Any]] = []
    for line in text.splitlines():
        stripped = line.strip().lstrip("-* ").strip()
        lowered = stripped.lower()
        if lowered.startswith("claim:") or stripped.startswith("宣告:"):
            claims.append({"text": stripped.split(":", 1)[1].strip()})
        elif lowered.startswith("recommendation:") or stripped.startswith("建議:") or stripped.startswith("建議："):
            recommendations.append({"text": stripped.split(":", 1)[1].strip() if ":" in stripped else stripped.split("：", 1)[1].strip()})
    return claims, recommendations


@dataclass
class ClaimCheck:
    claim_id: str
    kind: str
    text: str
    status: str = "insufficient"
    answer_status: str = "unknown"
    evidence_refs: List[str] = field(default_factory=list)
    rationale_ref: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    repaired_text: Optional[str] = None
    repair_status: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "claim_id": self.claim_id,
            "kind": self.kind,
            "text": self.text,
            "status": self.status,
            "answer_status": self.answer_status,
            "evidence_refs": list(self.evidence_refs),
            "rationale_ref": self.rationale_ref,
            "metadata": dict(self.metadata),
            "repaired_text": self.repaired_text,
            "repair_status": self.repair_status,
        }


@dataclass
class PostcheckResult:
    version: str = POSTCHECK_VERSION
    status: str = "not_requested"
    evidence_hash: str = ""
    evidence_count: int = 0
    claim_count: int = 0
    checked_count: int = 0
    checks: List[ClaimCheck] = field(default_factory=list)
    repairs: List[ClaimCheck] = field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    repair_count: int = 0
    max_repairs: int = 1
    usage: Dict[str, Any] = field(default_factory=dict)
    ledger: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "evidence_hash": self.evidence_hash,
            "evidence_count": self.evidence_count,
            "claim_count": self.claim_count,
            "checked_count": self.checked_count,
            "checks": [item.to_dict() for item in self.checks],
            "repairs": [item.to_dict() for item in self.repairs],
            "diagnostics": list(self.diagnostics),
            "repair_count": self.repair_count,
            "max_repairs": self.max_repairs,
            "usage": dict(self.usage),
            "ledger": dict(self.ledger),
            "provenance": dict(self.provenance),
        }


def _evidence_count(snapshot: Mapping[str, Any]) -> int:
    for key in ("evidence_refs", "cases", "turns", "items"):
        value = snapshot.get(key)
        if isinstance(value, list):
            return len(value)
    return sum(1 for key in snapshot if key not in {"coverage", "bundle_identity"})


def _question(claim: Mapping[str, Any], frozen: Mapping[str, Any]) -> SemanticQuestion:
    claim_id = str(claim["claim_id"])
    return SemanticQuestion(
        question_id=f"postcheck:{claim_id}:{stable_hash(claim['text'])[:12]}",
        axis_id="CONV",
        prompt=(
            "Check this generated claim against the frozen original evidence. "
            "Return supported only when the evidence directly supports it, contradicted "
            "when the evidence conflicts, overclaimed when it exceeds the evidence, "
            "and insufficient when the evidence cannot decide. Generated text is not evidence.\n"
            f"Generated {claim.get('kind', 'claim')}: {claim['text']}"
        ),
        answer_type="choice",
        case_id=claim_id,
        state_path="state.data.frozen_evidence",
        choices=CHECK_CHOICES,
        evidence_refs=tuple(str(item) for item in claim.get("evidence_refs", [])[:20]),
        group_version=POSTCHECK_VERSION,
        metadata={"evidence_hash": evidence_hash(frozen), "claim_id": claim_id},
    )


def _run_checks(
    claims: Sequence[Mapping[str, Any]],
    frozen: Mapping[str, Any],
    backend: Any,
    budget: SemanticBudget,
) -> Tuple[List[ClaimCheck], List[Dict[str, Any]], SemanticUsage, SemanticLedger, str]:
    questions = [_question(claim, frozen) for claim in claims]
    if not questions:
        return [], [], SemanticUsage(), SemanticLedger(), "not_applicable"
    state = SemanticState(
        state_id=f"postcheck-{evidence_hash(frozen)[:24]}",
        data={"frozen_evidence": frozen},
        case_ids=tuple(str(claim["claim_id"]) for claim in claims),
    )
    try:
        response = backend.evaluate(state, questions, budget=budget)
    except Exception as exc:
        checks = [
            ClaimCheck(
                claim_id=str(claim["claim_id"]),
                kind=str(claim.get("kind", "claim")),
                text=str(claim["text"]),
                status="insufficient",
                answer_status="failed",
                evidence_refs=list(claim.get("evidence_refs", [])),
            )
            for claim in claims
        ]
        return checks, [{"kind": "postcheck_exception", "status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]}], SemanticUsage(), SemanticLedger(), "failed"

    answers = getattr(response, "answers", {}) or {}
    diagnostics = list(getattr(response, "diagnostics", []) or [])
    checks: List[ClaimCheck] = []
    for claim, question in zip(claims, questions):
        answer = answers.get(question.question_id)
        value = getattr(answer, "value", None)
        if value in {"supported", "contradicted", "overclaimed"}:
            status = value
        else:
            status = "insufficient"
        checks.append(
            ClaimCheck(
                claim_id=str(claim["claim_id"]),
                kind=str(claim.get("kind", "claim")),
                text=str(claim["text"]),
                status=status,
                answer_status=str(getattr(answer, "status", "unknown")),
                evidence_refs=list(claim.get("evidence_refs", [])),
                rationale_ref=str(getattr(answer, "rationale_ref", "") or ""),
                metadata=dict(getattr(answer, "metadata", {}) or {}),
            )
        )
    usage = getattr(response, "usage", SemanticUsage())
    if not isinstance(usage, SemanticUsage):
        usage = SemanticUsage()
    ledger = getattr(response, "ledger", SemanticLedger())
    if not isinstance(ledger, SemanticLedger):
        ledger = SemanticLedger()
    return checks, diagnostics, usage, ledger, str(getattr(response, "status", "unknown"))


def check_generated_claims(
    evidence: Any,
    claims: Optional[Iterable[Any]] = None,
    recommendations: Optional[Iterable[Any]] = None,
    *,
    backend: Any = None,
    budget: Optional[SemanticBudget] = None,
    repair: Optional[Callable[..., Any]] = None,
    max_repairs: int = 1,
) -> PostcheckResult:
    """Check claims in one bounded batch and optionally perform one repair round."""

    if max_repairs < 0:
        raise ValueError("max_repairs must be non-negative")
    frozen = freeze_evidence(evidence)
    normalized = normalize_claims(claims, recommendations)
    result = PostcheckResult(
        status="not_applicable" if not normalized else "unknown",
        evidence_hash=evidence_hash(frozen),
        evidence_count=_evidence_count(frozen),
        claim_count=len(normalized),
        max_repairs=min(1, max_repairs),
        provenance={
            "postcheck_version": POSTCHECK_VERSION,
            "evidence_hash": evidence_hash(frozen),
            "evidence_frozen": True,
            "usage_scope": "postcheck_request_attempt_deduplicated",
        },
    )
    if not normalized:
        return result
    backend = backend or UnavailableSemanticBackend(reason="postcheck backend unavailable")
    budget = budget or SemanticBudget(max_requests=2, max_attempts=2, max_questions=max(1, len(normalized)), max_cases=max(1, len(normalized)), max_retries=0)
    checks, diagnostics, usage, ledger, response_status = _run_checks(normalized, frozen, backend, budget)
    result.checks = checks
    result.checked_count = sum(item.answer_status == "observed" for item in checks)
    result.diagnostics.extend(diagnostics)
    result.usage = usage.to_dict()
    result.ledger = ledger.to_dict()
    result.status = "complete" if response_status == "complete" and all(item.status == "supported" for item in checks) else (
        "partial" if response_status in {"complete", "partial"} else response_status
    )

    repair_targets = [item for item in checks if item.status in {"contradicted", "overclaimed"}]
    if repair is not None and repair_targets and result.max_repairs:
        try:
            repaired_claims: List[Dict[str, Any]] = []
            for item in repair_targets:
                try:
                    repaired = repair(item.to_dict(), frozen)
                except TypeError:
                    repaired = repair(item.text)
                if isinstance(repaired, Mapping):
                    text = repaired.get("text", repaired.get("claim", item.text))
                else:
                    text = repaired
                if isinstance(text, str) and text.strip() and text.strip() != item.text:
                    repaired_claims.append(
                        {
                            "claim_id": item.claim_id + ":repair",
                            "kind": item.kind,
                            "text": text.strip()[:12_000],
                            "evidence_refs": list(item.evidence_refs),
                        }
                    )
                    item.repaired_text = text.strip()[:12_000]
            if repaired_claims:
                result.repair_count = 1
                repaired_checks, repair_diagnostics, repair_usage, repair_ledger, repair_status = _run_checks(
                    repaired_claims, frozen, backend, budget
                )
                result.diagnostics.extend(repair_diagnostics)
                result.usage = SemanticUsage.sum([SemanticUsage.from_payload(result.usage), repair_usage]).to_dict()
                combined_ledger = SemanticLedger()
                combined_ledger.extend(ledger)
                combined_ledger.extend(repair_ledger)
                result.ledger = combined_ledger.to_dict()
                by_original = {item.claim_id: item for item in repaired_checks}
                result.repairs = repaired_checks
                for item in repair_targets:
                    repaired = by_original.get(item.claim_id + ":repair")
                    if repaired is not None:
                        item.repair_status = repaired.status
                result.provenance["repair_status"] = repair_status
        except Exception as exc:
            result.diagnostics.append({"kind": "repair_exception", "status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]})
    result.provenance["repair_count"] = result.repair_count
    result.provenance["max_repairs"] = result.max_repairs
    return result


def postcheck_analysis(
    evidence: Any,
    analysis: Any,
    **kwargs: Any,
) -> PostcheckResult:
    """Extract structured output from ``AgentAnalysis`` and check it."""

    claims = getattr(analysis, "claims", None)
    recommendations = getattr(analysis, "recommendations", None)
    if not claims and not recommendations:
        structured = getattr(analysis, "structured_output", {})
        if isinstance(structured, Mapping):
            claims = structured.get("claims", [])
            recommendations = structured.get("recommendations", [])
    if not claims and not recommendations:
        claims, recommendations = extract_generated_claims(getattr(analysis, "raw_response", ""))
    return check_generated_claims(evidence, claims, recommendations, **kwargs)


# Compatibility aliases for callers using a verb-first name.
check_claims = check_generated_claims
run_postcheck = check_generated_claims
