"""Bounded semantic cases, shared state, and versioned seven-axis questions."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .bundle import SessionBundle, redact_text
from .parser_base import Session
from .semantic_backend import (
    SemanticAnswer,
    SemanticBudget,
    SemanticQuestion,
    SemanticState,
    SemanticBudgetError,
    canonical_json,
    stable_hash,
)


AXIS_IDS = ("SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL")


QUESTION_GROUPS: Dict[str, Dict[str, Any]] = {
    "SNR": {
        "version": "snr-semantic-v1",
        "answer_type": "choice",
        "choices": ("relevant", "partially_relevant", "irrelevant"),
        "prompt": "Does the evidence retain the task-relevant information needed to judge this case, without treating raw verbosity as useful evidence?",
    },
    "STATE": {
        "version": "state-semantic-v1",
        "answer_type": "noul",
        "prompt": "What is the bounded probability that the recorded state is sufficient and non-contradictory for the decision in this case? Return only p(yes), not a confidence score.",
    },
    "CTX": {
        "version": "ctx-semantic-v1",
        "answer_type": "score",
        "score_levels": ("lost", "partial", "continuous"),
        "prompt": "Score whether the explicit task requirements and changes remain continuous through this case; do not infer memory from keyword repetition.",
    },
    "REACT": {
        "version": "react-semantic-v1",
        "answer_type": "choice",
        "choices": ("adapted", "repeated", "not_observable"),
        "prompt": "Classify the observed response to failures or expected polling: adapted, repeated an ineffective action, or not observable from the evidence.",
    },
    "DEPTH": {
        "version": "depth-semantic-v1",
        "answer_type": "noul",
        "prompt": "What is the bounded probability that the necessary verification evidence supports the claim at this observation cutoff? This is not a reasoning-length or confidence judgment.",
    },
    "CONV": {
        "version": "conv-semantic-v1",
        "answer_type": "score",
        "score_levels": ("unsupported", "partially_supported", "supported"),
        "prompt": "Score whether the delivery or completion claim is supported at its observation cutoff, separately from whether the task was ultimately successful.",
    },
    "TOOL": {
        "version": "tool-semantic-v1",
        "answer_type": "choice",
        "choices": ("effective", "inefficient", "unknown"),
        "prompt": "Classify whether the selected tool action and its result were used effectively for this case; do not penalize a justified target change as repetition.",
    },
}


def _clip(value: Any, limit: int = 4_000, depth: int = 0) -> Any:
    """Keep semantic state portable and bounded before transport."""

    if depth > 5:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        return {str(key): _clip(item, limit, depth + 1) for key, item in list(value.items())[:80]}
    if isinstance(value, (list, tuple)):
        return [_clip(item, limit, depth + 1) for item in list(value)[:80]]
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return str(value)[:limit]


def _string_list(value: Any, limit: int = 20) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(str(item) for item in value[:limit] if isinstance(item, str) and item)


@dataclass(frozen=True)
class SemanticCase:
    """One candidate case bounded by a source observation cutoff."""

    case_id: str
    kind: str
    turn_index: Optional[int] = None
    evidence_refs: Tuple[str, ...] = ()
    event_refs: Tuple[str, ...] = ()
    evidence_text: str = ""
    relations: Mapping[str, str] = field(default_factory=dict)
    observation_cutoff: Optional[str] = None
    observation_cutoff_sequence: Optional[int] = None
    ordering: str = "unknown"
    applicability: str = "applicable"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "case_id": self.case_id,
            "kind": self.kind,
            "turn_index": self.turn_index,
            "evidence_refs": list(self.evidence_refs),
            "event_refs": list(self.event_refs),
            "evidence_text": self.evidence_text,
            "relations": dict(self.relations),
            "observation_cutoff": self.observation_cutoff,
            "observation_cutoff_sequence": self.observation_cutoff_sequence,
            "ordering": self.ordering,
            "applicability": self.applicability,
        }


@dataclass(frozen=True)
class SemanticBatch:
    """One request-sized group sharing exactly one state snapshot."""

    batch_id: str
    stage: int
    state_hash: str
    questions: Tuple[SemanticQuestion, ...]
    case_ids: Tuple[str, ...]
    estimated_bytes: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "stage": self.stage,
            "state_hash": self.state_hash,
            "question_ids": [question.question_id for question in self.questions],
            "case_ids": list(self.case_ids),
            "estimated_bytes": self.estimated_bytes,
        }


def _case_from_mapping(raw: Mapping[str, Any], index: int) -> SemanticCase:
    case_id = str(raw.get("case_id", f"case-{index}"))
    turn_index = raw.get("turn_index")
    if not isinstance(turn_index, int) or isinstance(turn_index, bool):
        turn_index = None
    cutoff_sequence = raw.get("observation_cutoff_sequence")
    if not isinstance(cutoff_sequence, int) or isinstance(cutoff_sequence, bool):
        cutoff_sequence = None
    relations = raw.get("relations", {})
    if not isinstance(relations, Mapping):
        relations = {}
    return SemanticCase(
        case_id=case_id,
        kind=str(raw.get("kind", "observation_candidate")),
        turn_index=turn_index,
        evidence_refs=_string_list(raw.get("evidence_refs")),
        event_refs=_string_list(raw.get("event_refs")),
        evidence_text=str(raw.get("evidence_text", raw.get("text", "")))[:4_000],
        relations={str(key): str(value)[:120] for key, value in list(relations.items())[:20]},
        observation_cutoff=(str(raw["observation_cutoff"]) if raw.get("observation_cutoff") is not None else None),
        observation_cutoff_sequence=cutoff_sequence,
        ordering=str(raw.get("ordering", "unknown")),
        applicability=str(raw.get("applicability", "applicable")),
    )


def build_semantic_cases(
    session: Session,
    bundle: Optional[SessionBundle] = None,
    *,
    max_cases: int = 32,
) -> List[SemanticCase]:
    """Build broad, replayable candidates without inventing semantic edges."""

    if max_cases <= 0:
        raise ValueError("max_cases must be positive")
    if bundle is not None and bundle.cases:
        evidence_by_id = {
            str(item.get("ref_id")): item
            for item in bundle.evidence_refs
            if isinstance(item, Mapping) and item.get("ref_id")
        }
        cases: List[SemanticCase] = []
        for index, raw in enumerate(bundle.cases[:max_cases], 1):
            if not isinstance(raw, Mapping):
                continue
            item = dict(raw)
            if not item.get("evidence_text"):
                texts = []
                for ref_id in _string_list(item.get("evidence_refs")):
                    evidence = evidence_by_id.get(ref_id)
                    if isinstance(evidence, Mapping):
                        texts.append(str(evidence.get("text", "")))
                item["evidence_text"] = "\n".join(texts)[:4_000]
            cases.append(_case_from_mapping(item, index))
        if cases:
            return cases

    cases = []
    for index, turn in enumerate(session.turns[:max_cases], 1):
        parts = [turn.user_input, turn.assistant_output]
        parts.extend(call.output for call in turn.tool_calls if call.output)
        safe_text, _ = redact_text("\n".join(part for part in parts if part), 4_000)
        refs = tuple(
            str(event.get("event_id"))
            for event in turn.events
            if isinstance(event, Mapping) and event.get("event_id")
        )
        case_id = f"case-{hashlib.sha256(f'{session.source}:{session.id}:{turn.index}'.encode()).hexdigest()[:16]}"
        cases.append(
            SemanticCase(
                case_id=case_id,
                kind="turn_observation_candidate",
                turn_index=turn.index,
                event_refs=refs[:20],
                evidence_text=safe_text,
                applicability="applicable" if safe_text else "insufficient",
            )
        )
    return cases


def build_semantic_state(
    session: Session,
    bundle: Optional[SessionBundle],
    cases: Sequence[SemanticCase],
    *,
    max_cases: int = 32,
) -> SemanticState:
    """Create one shared state; case-specific paths remain question metadata."""

    selected = list(cases[:max_cases])
    coverage = dict(bundle.coverage) if bundle is not None else {}
    facts = bundle.facts if bundle is not None else {}
    # Only send bounded facts needed to interpret the cases.  In particular,
    # do not forward session metadata, raw source paths, or parser payloads.
    state_data = {
        "session": {
            "source": session.source,
            "model": session.model or None,
            "parser_version": session.parser_version,
            "turn_count": len(session.turns),
        },
        "coverage": _clip(coverage, 2_000),
        "facts": _clip(facts, 4_000),
        "cases": [_clip(case.to_dict(), 4_000) for case in selected],
    }
    state_id = stable_hash({"source": session.source, "session_id": session.id, "cases": [case.case_id for case in selected]})[:24]
    refs = tuple(ref for case in selected for ref in case.evidence_refs)
    return SemanticState(
        state_id=state_id,
        data=state_data,
        evidence_refs=tuple(dict.fromkeys(refs))[:100],
        case_ids=tuple(case.case_id for case in selected),
    )


def _question_for_case(axis_id: str, case: SemanticCase, *, stage: int = 1, depends_on: Sequence[str] = ()) -> SemanticQuestion:
    group = QUESTION_GROUPS[axis_id]
    stage_suffix = f":stage{stage}" if stage > 1 else ""
    question_id = f"{case.case_id}:{axis_id.lower()}:{group['version']}{stage_suffix}"
    applicability = case.applicability if case.applicability in {"applicable", "not_applicable", "insufficient", "unknown"} else "unknown"
    prompt = (
        f"Case {case.case_id} at state path state.cases.{case.case_id}. "
        f"Observation cutoff={case.observation_cutoff or 'unknown'}; evidence refs={','.join(case.evidence_refs) or 'none'}. "
        f"{group['prompt']} Use only the shared bounded state and this case; if evidence is insufficient, abstain."
    )
    return SemanticQuestion(
        question_id=question_id,
        axis_id=axis_id,
        prompt=prompt,
        answer_type=group["answer_type"],
        case_id=case.case_id,
        state_path=f"state.cases.{case.case_id}",
        choices=tuple(group.get("choices", ())),
        score_levels=tuple(group.get("score_levels", ())),
        applicability=applicability,
        evidence_refs=case.evidence_refs,
        stage=stage,
        depends_on=tuple(depends_on),
        group_version=str(group["version"]),
        metadata={"question_group": axis_id, "observation_cutoff": case.observation_cutoff},
    )


def build_semantic_questions(
    cases: Sequence[SemanticCase],
    *,
    axes: Sequence[str] = AXIS_IDS,
    include_dependent: bool = True,
) -> List[SemanticQuestion]:
    """Create all seven versioned axis groups with independent stage-one items."""

    unknown_axes = [axis for axis in axes if axis not in QUESTION_GROUPS]
    if unknown_axes:
        raise ValueError(f"unknown semantic axes: {unknown_axes}")
    questions: List[SemanticQuestion] = []
    for case in cases:
        for axis_id in axes:
            questions.append(_question_for_case(axis_id, case))
        if include_dependent:
            dependencies = tuple(question.question_id for question in questions[-len(tuple(axes)):])
            questions.append(
                SemanticQuestion(
                    question_id=f"{case.case_id}:cross-axis:semantic-v1",
                    axis_id="CONV",
                    prompt=(
                        f"Case {case.case_id} at state path state.cases.{case.case_id}: after considering the independent "
                        "axis judgments for this same case, is the delivery claim supported without overclaiming? "
                        "Abstain when those judgments are missing or contradictory."
                    ),
                    answer_type="choice",
                    case_id=case.case_id,
                    state_path=f"state.cases.{case.case_id}",
                    choices=("supported", "overclaimed", "unresolved"),
                    applicability=case.applicability,
                    evidence_refs=case.evidence_refs,
                    stage=2,
                    depends_on=dependencies,
                    group_version="conv-dependent-v1",
                    metadata={"question_group": "CONV", "dependent": True, "observation_cutoff": case.observation_cutoff},
                )
            )
    return questions


def build_dependent_questions(
    cases: Sequence[SemanticCase],
    first_stage_answers: Mapping[str, SemanticAnswer],
) -> List[SemanticQuestion]:
    """Return only dependent questions whose prerequisite judgments are usable."""

    output: List[SemanticQuestion] = []
    for case in cases:
        prerequisites = [
            question_id
            for question_id, answer in first_stage_answers.items()
            if question_id.startswith(f"{case.case_id}:") and answer.status == "observed" and answer.value is not None
        ]
        if not prerequisites:
            continue
        output.append(
            _question_for_case("CONV", case, stage=2, depends_on=prerequisites)
        )
    return output


def build_semantic_batches(
    state: SemanticState,
    questions: Sequence[SemanticQuestion],
    budget: Optional[SemanticBudget] = None,
) -> List[SemanticBatch]:
    """Split questions by request bytes while retaining one shared state per request."""

    budget = budget or SemanticBudget()
    state_payload = state.to_dict()
    state_bytes = len(canonical_json(state_payload))
    if state_bytes > budget.max_state_bytes:
        raise SemanticBudgetError("state byte budget exceeded")
    if len(set(question.question_id for question in questions)) != len(questions):
        raise ValueError("semantic question IDs must be unique")
    if len(set(question.case_id for question in questions)) > budget.max_cases:
        raise SemanticBudgetError("case budget exceeded")

    batches: List[SemanticBatch] = []
    current: List[SemanticQuestion] = []
    current_stage: Optional[int] = None

    def flush() -> None:
        nonlocal current, current_stage
        if not current:
            return
        question_payload = [question.to_dict() for question in current]
        request_payload = {"state": state_payload, "questions": question_payload}
        estimated = len(canonical_json(request_payload))
        batch_id = stable_hash({"state": state.snapshot_hash, "questions": [question.question_id for question in current]})[:24]
        batches.append(
            SemanticBatch(
                batch_id=batch_id,
                stage=int(current_stage or 1),
                state_hash=state.snapshot_hash,
                questions=tuple(current),
                case_ids=tuple(dict.fromkeys(question.case_id for question in current)),
                estimated_bytes=estimated,
            )
        )
        current = []
        current_stage = None

    for question in questions:
        encoded_question = canonical_json(question.to_dict())
        if len(encoded_question) > budget.max_question_bytes:
            raise SemanticBudgetError(f"question byte budget exceeded: {question.question_id}")
        if current_stage is not None and question.stage != current_stage:
            flush()
        candidate = current + [question]
        candidate_payload = {"state": state_payload, "questions": [item.to_dict() for item in candidate]}
        candidate_size = len(canonical_json(candidate_payload))
        if current and (len(candidate) > budget.max_questions or candidate_size > budget.max_request_bytes):
            flush()
            candidate = [question]
            candidate_payload = {"state": state_payload, "questions": [question.to_dict()]}
            candidate_size = len(canonical_json(candidate_payload))
        if len(candidate) > budget.max_questions or candidate_size > budget.max_request_bytes:
            raise SemanticBudgetError(f"request byte/question budget exceeded: {question.question_id}")
        current = candidate
        current_stage = question.stage
    flush()
    return batches


# Short aliases make the module convenient for callers and test fixtures.
build_cases = build_semantic_cases
build_questions = build_semantic_questions
build_batches = build_semantic_batches
