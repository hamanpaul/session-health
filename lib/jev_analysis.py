"""Semantic batch orchestration and report-safe Jev projections."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bundle import BundleLimits, SessionBundle, build_session_bundle
from .jev_questions import (
    AXIS_IDS,
    SemanticBatch,
    SemanticCase,
    build_semantic_batches,
    build_semantic_cases,
    build_semantic_questions,
    build_semantic_state,
)
from .parser_base import Session
from .semantic_backend import (
    GenericSemanticBackend,
    SemanticAnswer,
    SemanticBackend,
    SemanticBudget,
    SemanticBudgetError,
    SemanticCapabilities,
    SemanticLedger,
    SemanticQuestion,
    SemanticResponse,
    SemanticState,
    SemanticUsage,
    UnavailableSemanticBackend,
    build_default_backend,
    stable_hash,
)


@dataclass
class SemanticEvaluation:
    """Additive semantic result; offline facts remain in ``process_v2``."""

    version: str = "semantic-v1"
    status: str = "not_requested"
    live_status: str = "not_requested"
    backend: str = ""
    capabilities: Dict[str, Any] = field(default_factory=dict)
    state: Dict[str, Any] = field(default_factory=dict)
    cases: List[Dict[str, Any]] = field(default_factory=list)
    questions: List[Dict[str, Any]] = field(default_factory=list)
    answers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    raw_judgments: List[Dict[str, Any]] = field(default_factory=list)
    stages: List[Dict[str, Any]] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    ledger: Dict[str, Any] = field(default_factory=dict)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "live_status": self.live_status,
            "backend": self.backend,
            "capabilities": self.capabilities,
            "state": self.state,
            "cases": self.cases,
            "questions": self.questions,
            "answers": self.answers,
            "raw_judgments": self.raw_judgments,
            "stages": self.stages,
            "coverage": self.coverage,
            "diagnostics": self.diagnostics,
            "usage": self.usage,
            "ledger": self.ledger,
            "provenance": self.provenance,
        }


def _capabilities(backend: Any) -> Dict[str, Any]:
    value = getattr(backend, "capabilities", None)
    if isinstance(value, SemanticCapabilities):
        return value.to_dict()
    if hasattr(value, "to_dict"):
        try:
            result = value.to_dict()
            return dict(result) if isinstance(result, Mapping) else {}
        except Exception:
            return {}
    return {}


def _response_ledger(response: Optional[SemanticResponse], backend: Any) -> SemanticLedger:
    if response is not None and isinstance(response.ledger, SemanticLedger):
        return response.ledger
    ledger = getattr(backend, "ledger", None)
    return ledger if isinstance(ledger, SemanticLedger) else SemanticLedger()


def _usable_answer(answer: SemanticAnswer) -> bool:
    return answer.status == "observed" and answer.value is not None and answer.applicability == "applicable"


def _stage_questions(
    questions: Sequence[SemanticQuestion],
    stage: int,
    answers: Mapping[str, SemanticAnswer],
) -> Tuple[List[SemanticQuestion], List[SemanticQuestion]]:
    ready: List[SemanticQuestion] = []
    skipped: List[SemanticQuestion] = []
    for question in questions:
        if question.stage != stage:
            continue
        if all(dep in answers and _usable_answer(answers[dep]) for dep in question.depends_on):
            ready.append(question)
        else:
            skipped.append(question)
    return ready, skipped


def _append_response(
    response: SemanticResponse,
    answers: Dict[str, SemanticAnswer],
    diagnostics: List[Dict[str, Any]],
    raw_judgments: List[Dict[str, Any]],
) -> None:
    answers.update(response.answers)
    diagnostics.extend(response.diagnostics)
    for answer in response.answers.values():
        raw_judgments.append(answer.to_dict())


def _coverage(
    cases: Sequence[SemanticCase],
    questions: Sequence[SemanticQuestion],
    answers: Mapping[str, SemanticAnswer],
    diagnostics: Sequence[Mapping[str, Any]],
) -> Dict[str, Any]:
    axis_coverage: Dict[str, Dict[str, Any]] = {}
    for axis_id in AXIS_IDS:
        axis_questions = [question for question in questions if question.axis_id == axis_id]
        observed = [answers[question.question_id] for question in axis_questions if question.question_id in answers]
        usable = sum(1 for answer in observed if _usable_answer(answer))
        abstained = sum(1 for answer in observed if answer.status == "abstained" or answer.applicability != "applicable")
        axis_coverage[axis_id] = {
            "question_count": len(axis_questions),
            "answered_count": len(observed),
            "usable_count": usable,
            "abstained_count": abstained,
            "numerator": usable,
            "denominator": len(observed),
            "coverage": (usable / len(axis_questions)) if axis_questions else None,
            "status": "observed" if usable else "insufficient",
        }
    evaluated_cases = {
        question.case_id
        for question in questions
        if question.question_id in answers and _usable_answer(answers[question.question_id])
    }
    missing = sum(1 for item in diagnostics if item.get("kind") in {"missing_answer", "dependent_stage_skipped"})
    return {
        "case_count": len(cases),
        "evaluated_case_count": len(evaluated_cases),
        "question_count": len(questions),
        "answered_question_count": len(answers),
        "missing_or_skipped_count": missing,
        "axis_count": len(AXIS_IDS),
        "axes": axis_coverage,
        "semantic_coverage": (len(answers) / len(questions)) if questions else None,
        "label_status": "synthetic_expectations_only",
        "human_label_status": "pending_root_confirmation",
    }


def run_semantic_batch(
    state: SemanticState,
    cases: Sequence[SemanticCase],
    questions: Sequence[SemanticQuestion],
    backend: Any,
    *,
    budget: Optional[SemanticBudget] = None,
) -> SemanticEvaluation:
    """Run independent questions first, then at most the bounded dependent stage."""

    budget = budget or SemanticBudget()
    selected_questions = list(questions)
    if len(selected_questions) > budget.max_questions * max(1, budget.max_requests):
        selected_questions = selected_questions[: budget.max_questions * max(1, budget.max_requests)]
    answers: Dict[str, SemanticAnswer] = {}
    diagnostics: List[Dict[str, Any]] = []
    raw_judgments: List[Dict[str, Any]] = []
    stages: List[Dict[str, Any]] = []
    latest_response: Optional[SemanticResponse] = None
    overall_status = "complete"
    requests_used = 0
    request_bytes_used = 0
    for stage in range(1, budget.max_stages + 1):
        ready, skipped = _stage_questions(selected_questions, stage, answers)
        if stage == 1:
            # Stage-one questions have no prerequisites and are all independent.
            ready = [question for question in selected_questions if question.stage == 1]
            skipped = []
        if skipped:
            for question in skipped:
                diagnostics.append(
                    {
                        "kind": "dependent_stage_skipped",
                        "status": "insufficient",
                        "stage": stage,
                        "question_id": question.question_id,
                        "depends_on": list(question.depends_on),
                    }
                )
            overall_status = "partial"
        if not ready:
            if stage > 1:
                continue
            break
        try:
            batches = build_semantic_batches(state, ready, budget)
        except SemanticBudgetError as exc:
            diagnostics.append({"kind": "batch_budget_exceeded", "status": "failed", "stage": stage, "message": str(exc)})
            overall_status = "partial"
            break
        stage_record: Dict[str, Any] = {
            "stage": stage,
            "question_count": len(ready),
            "batch_count": 0,
            "batches": [],
        }
        for batch in batches:
            if requests_used >= budget.max_requests:
                diagnostics.append({"kind": "request_budget_exceeded", "status": "insufficient", "stage": stage})
                overall_status = "partial"
                break
            if request_bytes_used + batch.estimated_bytes > budget.max_total_bytes:
                diagnostics.append({"kind": "request_byte_budget_exceeded", "status": "insufficient", "stage": stage})
                overall_status = "partial"
                break
            try:
                latest_response = backend.evaluate(state, batch.questions, budget=budget)
            except SemanticBudgetError as exc:
                diagnostics.append({"kind": "request_budget_exceeded", "status": "failed", "stage": stage, "message": str(exc)})
                overall_status = "partial"
                break
            _append_response(latest_response, answers, diagnostics, raw_judgments)
            requests_used += 1
            request_bytes_used += batch.estimated_bytes
            stage_record["batch_count"] += 1
            stage_record["batches"].append(batch.to_dict())
            if latest_response.status in {"failed", "unknown"}:
                overall_status = "partial" if answers else latest_response.status
            elif latest_response.status == "deferred":
                overall_status = "deferred"
            elif latest_response.status == "partial":
                overall_status = "partial"
        stages.append(stage_record)
        if overall_status in {"deferred", "unknown"}:
            break

    ledger = _response_ledger(latest_response, backend)
    if not latest_response and isinstance(backend, UnavailableSemanticBackend):
        overall_status = "deferred"
    if latest_response is not None and latest_response.status == "deferred":
        overall_status = "deferred"
    elif len(answers) < len(selected_questions) and overall_status == "complete":
        overall_status = "partial"
    if not selected_questions:
        overall_status = "not_applicable"
    live_status = "mock" if _capabilities(backend).get("backend") == "mock" else (
        "deferred" if overall_status == "deferred" else overall_status
    )
    metadata = latest_response.metadata if latest_response is not None else {}
    provenance = dict(latest_response.provenance) if latest_response is not None else {}
    provenance.update(
        {
            "semantic_version": "semantic-v1",
            "state_hash": state.snapshot_hash,
            "questions_hash": stable_hash([question.to_dict() for question in selected_questions]),
            "rubric_hash": stable_hash(sorted({question.group_version for question in selected_questions})),
            "requested_model": _capabilities(backend).get("model") or None,
            "usage_scope": "request_attempt_deduplicated",
        }
    )
    return SemanticEvaluation(
        status=overall_status,
        live_status=str(metadata.get("live_status", live_status)),
        backend=str(_capabilities(backend).get("backend", type(backend).__name__)),
        capabilities=_capabilities(backend),
        state={"state_id": state.state_id, "snapshot_hash": state.snapshot_hash, "case_ids": list(state.case_ids)},
        cases=[case.to_dict() for case in cases],
        questions=[question.to_dict() for question in selected_questions],
        answers={question_id: answer.to_dict() for question_id, answer in answers.items()},
        raw_judgments=raw_judgments,
        stages=stages,
        coverage=_coverage(cases, selected_questions, answers, diagnostics),
        diagnostics=diagnostics,
        usage=ledger.usage().to_dict(),
        ledger=ledger.to_dict(),
        provenance=provenance,
    )


def evaluate_session_semantic(
    session: Session,
    *,
    bundle: Optional[SessionBundle] = None,
    backend: Any = None,
    budget: Optional[SemanticBudget] = None,
    offline: bool = False,
) -> SemanticEvaluation:
    """Build a portable semantic context and evaluate it with one backend."""

    budget = budget or SemanticBudget()
    if bundle is None:
        bundle = build_session_bundle(session, limits=BundleLimits(max_cases=budget.max_cases))
    cases = build_semantic_cases(session, bundle, max_cases=budget.max_cases)
    state = build_semantic_state(session, bundle, cases, max_cases=budget.max_cases)
    questions = build_semantic_questions(cases, include_dependent=True)
    if backend is None:
        backend = build_default_backend(offline=offline, model=session.model or "")
    return run_semantic_batch(state, cases, questions, backend, budget=budget)


def render_semantic_terminal(result: Optional[SemanticEvaluation], *, use_color: bool = True) -> str:
    if result is None:
        return ""
    coverage = result.coverage
    return "\n".join(
        [
            "Semantic Jev profile",
            f"  status: {result.status}  live: {result.live_status}  backend: {result.backend or 'unknown'}",
            f"  cases: {coverage.get('evaluated_case_count', 0)}/{coverage.get('case_count', 0)}  questions: {coverage.get('answered_question_count', 0)}/{coverage.get('question_count', 0)}",
            f"  usage: {result.usage.get('total_tokens') if result.usage.get('total_tokens') is not None else 'unknown'} total tokens",
        ]
    )


def render_semantic_html(result: Optional[SemanticEvaluation]) -> str:
    if result is None:
        return ""
    import html
    payload = html.escape(__import__("json").dumps(result.to_dict(), indent=2, ensure_ascii=False))
    return f'<div class="agent-analysis"><h2>Semantic Jev profile</h2><pre>{payload}</pre></div>'


# Friendly aliases for direct callers.
evaluate_semantic = evaluate_session_semantic
SemanticResult = SemanticEvaluation
