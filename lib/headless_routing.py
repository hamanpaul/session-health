"""Bounded consensus policy for headless analyzer selection."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from .jev_routing import AnalysisRequest, candidate_id, eligible_candidates


POLICY_VERSION = "headless-consensus-v1"
ABSTENTIONS = {"no_suitable_model", "insufficient_model_evidence", "abstain"}


@dataclass
class HeadlessDecision:
    status: str = "no_suitable_model"
    selected: Any = None
    candidate_id: Optional[str] = None
    eligible_candidate_ids: List[str] = field(default_factory=list)
    eligibility: List[Any] = field(default_factory=list)
    judge_receipts: List[Dict[str, Any]] = field(default_factory=list)
    tie_policy: str = "priority_then_identity"
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "policy_version": POLICY_VERSION,
            "status": self.status,
            "candidate_id": self.candidate_id,
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "eligibility": [item.to_dict() if hasattr(item, "to_dict") else item for item in self.eligibility],
            "judge_receipts": list(self.judge_receipts),
            "tie_policy": self.tie_policy,
            "diagnostics": list(self.diagnostics),
        }


def judge_prompt(request: AnalysisRequest, candidates: Sequence[Any]) -> str:
    cards = []
    for item in candidates:
        cards.append(
            {
                "candidate_id": candidate_id(item),
                "executor": getattr(item, "executor", ""),
                "provider": getattr(item, "provider", ""),
                "model_id": getattr(item, "model_id", ""),
                "capabilities": list(getattr(item, "capabilities", ()) or ()),
                "capability_evidence": getattr(item, "capability_evidence", {}) or {},
                "availability": getattr(item, "availability", {}) or {},
                "priority": getattr(item, "priority", 100),
            }
        )
    return (
        "Choose one eligible analyzer for the bounded session-health task. "
        "Return exactly one JSON object with candidate_id, confidence (0..1), reason, and evidence_refs. "
        "Use insufficient_model_evidence to abstain. Model names alone are not capability evidence.\n"
        + json.dumps({"request": request.to_dict(), "candidates": cards}, ensure_ascii=False, sort_keys=True)
    )


def _receipt(raw: Any, *, judge_id: str, eligible_ids: set[str]) -> Dict[str, Any]:
    payload = raw if isinstance(raw, Mapping) else {}
    choice = payload.get("candidate_id")
    status = "valid"
    if not isinstance(choice, str):
        choice = "insufficient_model_evidence"
        status = "invalid"
    elif choice not in eligible_ids and choice not in ABSTENTIONS:
        status = "illegal_candidate"
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
        confidence = None
    refs = payload.get("evidence_refs", [])
    refs = [str(item)[:300] for item in refs[:20]] if isinstance(refs, list) else []
    return {
        "judge_id": judge_id,
        "status": status,
        "candidate_id": choice,
        "confidence": confidence,
        "reason": str(payload.get("reason", ""))[:500],
        "evidence_refs": refs,
    }


def select_headless_model(
    candidates: Sequence[Any],
    request: AnalysisRequest,
    *,
    judge: Callable[[Any, str], Any],
    jev_receipt: Optional[Mapping[str, Any]] = None,
    max_judges: int = 3,
) -> HeadlessDecision:
    """Hard-filter candidates, collect bounded votes, then resolve consensus."""

    accepted, ledger = eligible_candidates(candidates, request, allow_unknown=False)
    decision = HeadlessDecision(
        eligible_candidate_ids=[candidate_id(item) for item in accepted],
        eligibility=ledger,
    )
    if not accepted:
        decision.diagnostics.append({"kind": "no_active_eligible_model", "status": "insufficient"})
        return decision
    eligible_ids = set(decision.eligible_candidate_ids)
    prompt = judge_prompt(request, accepted)
    for item in accepted[: min(2, max_judges)]:
        try:
            raw = judge(item, prompt)
            decision.judge_receipts.append(_receipt(raw, judge_id=candidate_id(item), eligible_ids=eligible_ids))
        except Exception as exc:
            decision.judge_receipts.append(
                {"judge_id": candidate_id(item), "status": "failed", "candidate_id": None, "reason": f"{type(exc).__name__}: {exc}"[:500]}
            )
    if jev_receipt is not None and len(decision.judge_receipts) < max_judges:
        decision.judge_receipts.append(_receipt(jev_receipt, judge_id="jev", eligible_ids=eligible_ids))

    counts: Dict[str, int] = {}
    for receipt in decision.judge_receipts:
        choice = receipt.get("candidate_id")
        if receipt.get("status") == "valid" and choice in eligible_ids:
            counts[str(choice)] = counts.get(str(choice), 0) + 1
    if not counts:
        decision.diagnostics.append({"kind": "all_judges_abstained_or_failed", "status": "insufficient"})
        return decision
    best = max(counts.values())
    tied = {identity for identity, count in counts.items() if count == best}
    selected = next(item for item in accepted if candidate_id(item) in tied)
    if len(tied) > 1:
        decision.diagnostics.append({"kind": "judge_tie", "status": "resolved", "policy": decision.tie_policy})
    decision.status = "selected"
    decision.selected = selected
    decision.candidate_id = candidate_id(selected)
    return decision
