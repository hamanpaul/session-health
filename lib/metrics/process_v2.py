"""Observable, no-LLM ``process-v2`` metrics.

The legacy scorer remains intentionally separate.  This module reports what a
portable session artifact can actually show: ratios carry explicit
numerators/denominators, missing evidence is null, and observations are not
presented as semantic or correctness judgments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..bundle import SessionBundle
from ..parser_base import Session, ToolCall, Turn
from .snr import analyze_snr


PROCESS_PROFILE = "process-v2"
PROCESS_VERSION = "process-v2.1"
AXIS_IDS = ("SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL")


@dataclass
class ObservableMetric:
    """A bounded ratio with explicit applicability and evidence coverage."""

    numerator: Optional[int] = None
    denominator: Optional[int] = None
    excluded_count: int = 0
    value: Optional[float] = None
    coverage: Optional[float] = None
    applicability: str = "unknown"
    status: str = "unknown"
    reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "numerator": self.numerator,
            "denominator": self.denominator,
            "excluded_count": self.excluded_count,
            "value": self.value,
            "coverage": self.coverage,
            "applicability": self.applicability,
            "status": self.status,
            "reason": self.reason,
        }


@dataclass
class AxisObservation:
    axis_id: str
    metric: ObservableMetric = field(default_factory=ObservableMetric)
    observed_facts: Dict[str, Any] = field(default_factory=dict)
    inference: Dict[str, Any] = field(default_factory=dict)
    evidence_refs: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "axis_id": self.axis_id,
            "version": PROCESS_VERSION,
            "metric": self.metric.to_dict(),
            "observed_facts": self.observed_facts,
            "inference": self.inference,
            "evidence_refs": self.evidence_refs,
            "notes": self.notes,
        }


@dataclass
class ProcessV2Result:
    profile: str = PROCESS_PROFILE
    version: str = PROCESS_VERSION
    status: str = "complete"
    axes: Dict[str, AxisObservation] = field(default_factory=dict)
    observed_facts: Dict[str, Any] = field(default_factory=dict)
    inference: Dict[str, Any] = field(default_factory=dict)
    external_outcome: Dict[str, Any] = field(default_factory=dict)
    coverage: Dict[str, Any] = field(default_factory=dict)
    processing: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "profile": self.profile,
            "version": self.version,
            "status": self.status,
            "axes": {axis_id: axis.to_dict() for axis_id, axis in self.axes.items()},
            "observed_facts": self.observed_facts,
            "inference": self.inference,
            "external_outcome": self.external_outcome,
            "coverage": self.coverage,
            "processing": self.processing,
        }

    @property
    def dimensions(self) -> Dict[str, Optional[float]]:
        return {axis_id: axis.metric.value for axis_id, axis in self.axes.items()}


def _ratio(numerator: int, denominator: int, *, excluded: int = 0, applicability: str = "applicable", reason: str = "") -> ObservableMetric:
    if denominator <= 0:
        return ObservableMetric(
            numerator=None,
            denominator=None,
            excluded_count=excluded,
            value=None,
            coverage=None,
            applicability="not_applicable",
            status="not_applicable",
            reason=reason or "no applicable observations",
        )
    return ObservableMetric(
        numerator=numerator,
        denominator=denominator,
        excluded_count=excluded,
        value=round(max(0.0, min(1.0, numerator / denominator)), 6),
        coverage=round(denominator / max(1, denominator + excluded), 6),
        applicability=applicability,
        status="observed",
        reason=reason,
    )


def _not_applicable(reason: str, excluded: int = 0) -> ObservableMetric:
    return ObservableMetric(
        numerator=None,
        denominator=None,
        excluded_count=excluded,
        value=None,
        coverage=None,
        applicability="not_applicable",
        status="not_applicable",
        reason=reason,
    )


def _calls(session: Session) -> List[ToolCall]:
    return [call for turn in session.turns for call in turn.tool_calls]


def _status(call: ToolCall) -> str:
    if call.success is True or call.exit_code == 0:
        return "success"
    if call.success is False or (call.exit_code is not None and call.exit_code != 0):
        return "failed"
    if call.status in {"success", "failed", "unknown"}:
        return call.status
    return "unknown"


def _event_refs(bundle: SessionBundle | None, axis_hint: str = "") -> List[str]:
    if bundle is None:
        return []
    refs: List[str] = []
    for event in bundle.events[:100]:
        kind = str(event.get("kind", ""))
        if not axis_hint or axis_hint.lower() in kind.lower() or kind in {"tool_call", "tool_result", "session_event"}:
            if event.get("event_id"):
                refs.append(str(event["event_id"]))
    return refs[:20]


def _snr_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    calls = _calls(session)
    outputs = [call for call in calls if call.output]
    if not outputs:
        return AxisObservation(
            "SNR",
            _not_applicable("no tool output characters were observed"),
            {"tool_outputs": 0, "total_chars": 0},
            {"quality_judgment": None},
            _event_refs(bundle, "result"),
        )
    total = 0
    clean = 0
    duplicate = 0
    ansi = 0
    for turn in session.turns:
        result = analyze_snr(turn)
        total += result.total_chars
        clean += max(0, result.total_chars - result.noise_chars)
        duplicate += result.duplicate_chars
        ansi += result.ansi_chars
    metric = _ratio(clean, total, reason="clean observed output characters / output characters")
    return AxisObservation(
        "SNR",
        metric,
        {"tool_outputs": len(outputs), "total_chars": total, "clean_chars": clean, "duplicate_chars": duplicate, "ansi_chars": ansi},
        {"quality_judgment": None, "method": "observable_noise_character_ratio"},
        _event_refs(bundle, "result"),
        ["Does not judge task relevance or semantic sufficiency."],
    )


def _state_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    applicable = [turn for turn in session.turns if turn.tool_calls]
    if not applicable:
        return AxisObservation("STATE", _not_applicable("no tool-bearing turns were observed"), {"applicable_turns": 0}, {"quality_judgment": None}, _event_refs(bundle))
    fields = ("cwd_present", "exit_code_present", "permission_present", "git_present")
    numerator = sum(1 for turn in applicable for field_name in fields if turn.context_meta.get(field_name) is True)
    denominator = len(applicable) * len(fields)
    observed_turns = sum(1 for turn in applicable if any(turn.context_meta.get(name) is True for name in fields))
    metric = _ratio(numerator, denominator, reason="observed state fields / applicable state fields")
    return AxisObservation(
        "STATE",
        metric,
        {"applicable_turns": len(applicable), "observed_turns": observed_turns, "fields": list(fields), "present_fields": numerator},
        {"quality_judgment": None},
        _event_refs(bundle),
        ["Missing state is counted as an observed gap, not as an unknown success."],
    )


def _ctx_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    transitions = session.turns[1:]
    if not transitions:
        return AxisObservation("CTX", _not_applicable("fewer than two turns; continuity cannot be observed"), {"transitions": 0}, {"memory_judgment": None}, _event_refs(bundle))
    # Only count explicit continuity carriers.  Keyword repetition and text
    # length are intentionally not treated as memory evidence.
    explicit = 0
    for turn in transitions:
        has_ref = bool(turn.context_meta.get("task_ref") or turn.context_meta.get("session_ref"))
        has_continuity_event = any(
            str(event.get("type", "")) in {"task_ref", "task_continued", "context_restored", "context_compacted"}
            for event in turn.events
            if isinstance(event, dict)
        )
        if has_ref or has_continuity_event:
            explicit += 1
    metric = _ratio(explicit, len(transitions), reason="turn transitions with explicit continuity evidence")
    return AxisObservation(
        "CTX",
        metric,
        {"transitions": len(transitions), "explicit_continuity_transitions": explicit, "compaction_events": session.context_compacted_count},
        {"memory_judgment": None, "method": "explicit_refs_or_lifecycle_events_only"},
        _event_refs(bundle),
        ["User-text keyword overlap is not used as proof of memory or goal retention."],
    )


def _react_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    calls = _calls(session)
    known_indices = [index for index, call in enumerate(calls) if _status(call) != "unknown"]
    if not known_indices:
        return AxisObservation("REACT", _not_applicable("no tool outcome was known", excluded=len(calls)), {"total_calls": len(calls), "known_calls": 0, "recovered_failures": 0}, {"recovery_judgment": None}, _event_refs(bundle))
    recovered = 0
    for index in known_indices:
        call = calls[index]
        if _status(call) != "failed":
            continue
        for later in calls[index + 1:]:
            if later.name == call.name and later.arguments != call.arguments and _status(later) == "success":
                recovered += 1
                break
    successful = sum(1 for call in calls if _status(call) == "success")
    numerator = successful + recovered
    denominator = len(known_indices)
    metric = _ratio(numerator, denominator, excluded=len(calls) - denominator, reason="successful outcomes plus changed-parameter recovered failures / known outcomes")
    return AxisObservation(
        "REACT",
        metric,
        {"total_calls": len(calls), "known_calls": denominator, "successful_calls": successful, "failed_calls": denominator - successful, "recovered_failures": recovered},
        {"recovery_judgment": None, "method": "outcome_and_changed_retry_observation"},
        _event_refs(bundle),
        ["A failed call without a later changed successful retry remains a failed observation."],
    )


def _depth_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    action_turns = [turn for turn in session.turns if turn.tool_calls]
    if not action_turns:
        return AxisObservation("DEPTH", _not_applicable("no action-bearing turns were observed"), {"action_turns": 0, "verification_signals": 0}, {"reasoning_judgment": None}, _event_refs(bundle))
    verification_signals = 0
    signal_kinds: List[str] = []
    for turn in action_turns:
        found = False
        for event in turn.events:
            kind = str(event.get("type", "")).lower() if isinstance(event, dict) else ""
            if any(token in kind for token in ("check", "test", "verify", "validation", "evidence")):
                found = True
                signal_kinds.append(kind)
        for call in turn.tool_calls:
            name = call.name.lower()
            if any(token in name for token in ("pytest", "test", "verify", "check")):
                found = True
                signal_kinds.append(call.name)
        if found:
            verification_signals += 1
    metric = _ratio(verification_signals, len(action_turns), reason="action turns with observable check/verification signals")
    return AxisObservation(
        "DEPTH",
        metric,
        {"action_turns": len(action_turns), "verification_signals": verification_signals, "signal_kinds": signal_kinds[:20]},
        {"reasoning_judgment": None, "method": "visible_verification_evidence_only"},
        _event_refs(bundle, "check"),
        ["Private chain-of-thought and assistant text length are not used."],
    )


def _conv_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    if session.task_started_count <= 0:
        return AxisObservation(
            "CONV",
            _not_applicable("no explicit task lifecycle start was observed"),
            {"task_started": session.task_started_count, "task_complete": session.task_complete_count, "aborts": session.turn_aborted_count},
            {"delivery_judgment": None, "task_success": None},
            _event_refs(bundle),
            ["Absence of abort is not evidence of delivery completion."],
        )
    metric = _ratio(
        min(session.task_complete_count, session.task_started_count),
        session.task_started_count,
        reason="explicit task completion lifecycle markers / task starts",
    )
    return AxisObservation(
        "CONV",
        metric,
        {"task_started": session.task_started_count, "task_complete": session.task_complete_count, "aborts": session.turn_aborted_count},
        {"delivery_judgment": None, "task_success": None},
        _event_refs(bundle),
        ["Lifecycle completion is reported separately from external correctness."],
    )


def _tool_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    calls = _calls(session)
    known = [call for call in calls if _status(call) != "unknown"]
    if not known:
        return AxisObservation("TOOL", _not_applicable("no tool outcome was known", excluded=len(calls)), {"total_calls": len(calls), "known_calls": 0, "redundant_calls": 0}, {"efficiency_judgment": None}, _event_refs(bundle))
    successful = sum(1 for call in known if _status(call) == "success")
    redundant = 0
    for previous, current in zip(calls, calls[1:]):
        if previous.name == current.name and previous.arguments == current.arguments:
            redundant += 1
    metric = _ratio(successful, len(known), excluded=len(calls) - len(known), reason="successful known outcomes / known outcomes")
    return AxisObservation(
        "TOOL",
        metric,
        {"total_calls": len(calls), "known_calls": len(known), "successful_calls": successful, "failed_calls": len(known) - successful, "redundant_calls": redundant},
        {"efficiency_judgment": None, "method": "outcome_and_exact_adjacent_duplicate_observation"},
        _event_refs(bundle, "tool"),
        ["Unknown outcomes are excluded instead of being counted as successes."],
    )


def join_external_outcome(session: Session | SessionBundle, artifact: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Join an optional fixture only when explicit identity fields match."""

    normalized = session.to_session() if isinstance(session, SessionBundle) else session
    candidates = list(artifact) if isinstance(artifact, (list, tuple)) else [artifact]
    session_id = normalized.id
    task_id = normalized.metadata.get("task_id") or normalized.metadata.get("taskId")
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_session_id = candidate.get("session_id", candidate.get("sessionId"))
        candidate_task_id = candidate.get("task_id", candidate.get("taskId"))
        matches = False
        if candidate_session_id is not None and session_id and str(candidate_session_id) == str(session_id):
            matches = True
        if candidate_task_id is not None and task_id and str(candidate_task_id) == str(task_id):
            matches = True
        if not matches:
            continue
        return {
            "status": "matched",
            "identity_match": True,
            "session_id": session_id,
            "task_id": task_id,
            "verdict": candidate.get("verdict"),
            "observed_at": candidate.get("observed_at", candidate.get("timestamp")),
            "version": candidate.get("version"),
            "authority": candidate.get("authority"),
            "source_refs": list(candidate.get("source_refs", candidate.get("refs", [])) or []),
            "interpretation": "external_verdict_only; not an internally established correctness claim",
        }
    return {
        "status": "not_joined",
        "identity_match": False,
        "session_id": session_id,
        "task_id": task_id,
        "verdict": None,
        "reason": "no exact session_id or task_id match",
    }


def analyze_process_v2(
    session: Session,
    bundle: SessionBundle | None = None,
    external_outcome: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> ProcessV2Result:
    """Produce all seven observable process-v2 axes without external calls."""

    axis_builders = (_snr_axis, _state_axis, _ctx_axis, _react_axis, _depth_axis, _conv_axis, _tool_axis)
    axes = {axis.axis_id: axis for axis in (builder(session, bundle) for builder in axis_builders)}
    result = ProcessV2Result(
        axes=axes,
        observed_facts={
            "session_id": session.id,
            "source": session.source,
            "turn_count": len(session.turns),
            "parser_version": session.parser_version,
            "diagnostics": len(session.diagnostics),
        },
        inference={
            "semantic_judgment": None,
            "correctness_judgment": None,
            "legacy_composite": "separate_profile",
        },
        external_outcome=join_external_outcome(session, external_outcome) if external_outcome is not None else {"status": "not_requested"},
        coverage={
            "axis_count": len(axes),
            "axis_ids": list(AXIS_IDS),
            "observed_axis_count": sum(1 for axis in axes.values() if axis.metric.status == "observed"),
            "not_applicable_axis_count": sum(1 for axis in axes.values() if axis.metric.status == "not_applicable"),
        },
        processing={
            "mode": "offline",
            "network": "disabled_by_contract",
            "model": "not_requested",
            "sdk": "not_used",
        },
    )
    return result


score_process_v2 = analyze_process_v2
process_v2_score = analyze_process_v2
