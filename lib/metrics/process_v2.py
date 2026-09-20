"""Observable, no-LLM ``process-v2`` metrics.

The legacy scorer remains intentionally separate.  This module reports what a
portable session artifact can actually show: ratios carry explicit
numerators/denominators, missing evidence is null, and observations are not
presented as semantic or correctness judgments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import shlex
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from ..bundle import SessionBundle
from ..parser_base import Session, ToolCall, Turn
from .snr import SNRResult, analyze_snr


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


def _unknown(reason: str, excluded: int = 0) -> ObservableMetric:
    """Represent an in-scope axis whose evidence cannot support a ratio."""

    return ObservableMetric(
        numerator=None,
        denominator=None,
        excluded_count=excluded,
        value=None,
        coverage=None,
        applicability="unknown",
        status="unknown",
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
        outputs = [
            call
            for turn in session.turns
            if turn.snr_facts and int(turn.snr_facts.get("total_chars", 0) or 0) > 0
            for call in turn.tool_calls[:1]
        ]
    if not outputs:
        return AxisObservation(
            "SNR",
            _not_applicable("no tool output characters were observed"),
            {"tool_outputs": 0, "total_chars": 0},
            {"quality_judgment": None},
            _event_refs(bundle, "result"),
        )
    total = 0
    visible_total = 0
    clean = 0
    duplicate = 0
    ansi = 0
    for turn in session.turns:
        result = _authoritative_snr(turn)
        visible_result = analyze_snr(turn) if turn.snr_facts else result
        observed_chars = sum(len(call.output or "") for call in turn.tool_calls)
        raw_chars = max(int(turn.raw_tool_output_chars or 0), observed_chars)
        if turn.snr_facts:
            raw_chars = int(turn.snr_facts.get("total_chars", raw_chars))
        total += raw_chars
        visible_total += visible_result.total_chars
        # A replay may retain only bounded evidence text.  Keep the original
        # observed denominator from the portable fact instead of silently
        # changing the statistic to the truncated payload length.
        clean += max(0, raw_chars - result.noise_chars)
        duplicate += result.duplicate_chars
        ansi += result.ansi_chars
    metric = _ratio(clean, total, reason="clean observed output characters / output characters")
    return AxisObservation(
        "SNR",
        metric,
        {"tool_outputs": len(outputs), "total_chars": total, "visible_chars": visible_total, "clean_chars": clean, "duplicate_chars": duplicate, "ansi_chars": ansi, "evidence_truncated_chars": max(0, total - visible_total)},
        {"quality_judgment": None, "method": "observable_noise_character_ratio"},
        _event_refs(bundle, "result"),
        ["Does not judge task relevance or semantic sufficiency."],
    )


def _authoritative_snr(turn: Turn) -> SNRResult:
    """Use a portable full-output fact snapshot when replaying a bundle.

    Bundle evidence is intentionally truncated.  Re-running the detector over
    that excerpt would change duplicate/ANSI counts and make deterministic
    metrics depend on whether the input was parsed directly or replayed.
    """

    facts = turn.snr_facts
    required = ("total_chars", "noise_chars", "ansi_chars", "progress_chars", "duplicate_chars")
    if facts and all(
        isinstance(facts.get(name), int) and not isinstance(facts.get(name), bool)
        and facts.get(name, 0) >= 0
        for name in required
    ):
        total = int(facts["total_chars"])
        noise = min(total, int(facts["noise_chars"]))
        return SNRResult(
            total_chars=total,
            noise_chars=noise,
            ansi_chars=int(facts["ansi_chars"]),
            progress_chars=int(facts["progress_chars"]),
            duplicate_chars=int(facts["duplicate_chars"]),
            score=max(0.0, (1.0 - (noise / total)) * 100) if total else 100.0,
        )
    return analyze_snr(turn)


def _state_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    applicable = [turn for turn in session.turns if turn.tool_calls]
    if not applicable:
        return AxisObservation("STATE", _not_applicable("no tool-bearing turns were observed"), {"applicable_turns": 0}, {"quality_judgment": None}, _event_refs(bundle))
    fields = ("cwd_present", "exit_code_present", "permission_present", "git_present")
    present: Dict[str, int] = {field_name: 0 for field_name in fields}
    observed: Dict[str, int] = {field_name: 0 for field_name in fields}
    for turn in applicable:
        for call in turn.tool_calls:
            metadata = call.result_metadata if isinstance(call.result_metadata, Mapping) else {}
            values = {
                "cwd_present": bool(turn.context_meta.get("cwd_present") or metadata.get("cwd") or metadata.get("working_directory") or metadata.get("workingDirectory") or session.cwd),
                "exit_code_present": call.exit_code is not None or turn.context_meta.get("exit_code_present") is True,
                "permission_present": bool(turn.context_meta.get("permission_present") or metadata.get("permission") or metadata.get("permissions")),
                "git_present": bool(turn.context_meta.get("git_present") or metadata.get("git") or metadata.get("git_status")),
            }
            for field_name, value in values.items():
                # Missing fields are unknown, not negative observations.  A
                # source that actually emits a field participates in its own
                # denominator; there is no universal four-field penalty.
                if value or field_name in turn.context_meta or field_name in metadata or (field_name == "exit_code_present" and call.exit_code is not None):
                    observed[field_name] += 1
                    if value:
                        present[field_name] += 1
    numerator = sum(present.values())
    denominator = sum(observed.values())
    if denominator <= 0:
        metric = _unknown("tool turns were observed, but no typed state fields were emitted", excluded=len(applicable))
    else:
        metric = _ratio(numerator, denominator, excluded=max(0, len(applicable) - denominator), reason="present typed state fields / fields actually emitted")
    observed_turns = sum(1 for turn in applicable if any(turn.context_meta.get(name) is True for name in fields) or any(call.exit_code is not None for call in turn.tool_calls))
    return AxisObservation(
        "STATE",
        metric,
        {"applicable_turns": len(applicable), "observed_turns": observed_turns, "fields": list(fields), "present_fields": numerator, "observed_fields": observed, "field_presence": present},
        {"quality_judgment": None, "method": "typed_result_and_adapter_context_fields"},
        _event_refs(bundle),
        ["Fields absent from a source record remain unknown and are not treated as a failed universal checklist."],
    )


def _ctx_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    transitions = session.turns[1:]
    if not transitions:
        return AxisObservation("CTX", _not_applicable("fewer than two turns; continuity cannot be observed"), {"transitions": 0}, {"memory_judgment": None}, _event_refs(bundle))
    # Only explicit continuity or explicit loss evidence is measurable.
    # Compaction is a transition/event, not proof that continuity succeeded.
    positive = 0
    negative = 0
    related = 0
    for turn in transitions:
        has_ref = bool(turn.context_meta.get("task_ref") or turn.context_meta.get("session_ref"))
        event_types = {
            str(event.get("type", ""))
            for event in turn.events
            if isinstance(event, Mapping)
        }
        has_positive = has_ref or bool(event_types & {"task_ref", "task_continued", "context_restored"})
        has_negative = bool(event_types & {"context_lost", "task_context_lost", "context_reset"})
        if has_positive or has_negative:
            related += 1
            if has_positive and not has_negative:
                positive += 1
            elif has_negative and not has_positive:
                negative += 1
    if related == 0:
        metric = _unknown("multiple turns exist, but no explicit continuity or loss evidence was observed", excluded=len(transitions))
    else:
        metric = _ratio(positive, related, excluded=len(transitions) - related, reason="explicit continuity observations / related transitions")
    return AxisObservation(
        "CTX",
        metric,
        {"transitions": len(transitions), "related_transitions": related, "explicit_continuity_transitions": positive, "explicit_loss_transitions": negative, "compaction_events": session.context_compacted_count},
        {"memory_judgment": None, "method": "explicit_refs_or_lifecycle_events_only"},
        _event_refs(bundle),
        ["Compaction alone is not positive continuity evidence; user-text overlap is not used as proof of memory."],
    )


def _lifecycle_events(session: Session, bundle: SessionBundle | None) -> List[Mapping[str, Any]]:
    """Return lifecycle evidence from the same source for direct and bundle APIs."""

    if bundle is not None:
        metric_facts = bundle.facts.get("metric_facts", {}) if isinstance(bundle.facts, Mapping) else {}
        lifecycle_facts = metric_facts.get("lifecycle") if isinstance(metric_facts, Mapping) else None
        if isinstance(metric_facts, Mapping) and metric_facts.get("complete") is True and isinstance(lifecycle_facts, list):
            return [item for item in lifecycle_facts if isinstance(item, Mapping)]
        return [
            event.get("payload", {})
            for event in bundle.events
            if event.get("kind") == "session_event"
            and isinstance(event.get("payload"), Mapping)
        ]
    if session.event_log:
        return [
            event.get("payload", {})
            for event in session.event_log
            if event.get("kind") == "session_event"
            and isinstance(event.get("payload"), Mapping)
        ]
    return [
        event
        for turn in session.turns
        for event in turn.events
        if isinstance(event, Mapping)
    ]


def _command_text(call: ToolCall) -> str:
    if not isinstance(call.arguments, Mapping):
        return ""
    return str(call.arguments.get("command", call.arguments.get("cmd", "")) or "").strip()


def _related_retry(previous: ToolCall, later: ToolCall) -> bool:
    """Conservatively identify a changed retry in the same episode."""

    if previous.name != later.name or previous.arguments == later.arguments:
        return False
    previous_command = _command_text(previous)
    later_command = _command_text(later)
    if previous_command or later_command:
        if not previous_command or not later_command:
            return False
        try:
            previous_tokens = shlex.split(previous_command)
            later_tokens = shlex.split(later_command)
        except ValueError:
            return False
        if not previous_tokens or not later_tokens:
            return False
        # A changed argument to the same executable is related.  Different
        # shell commands (e.g. ``false`` then ``pwd``) are separate episodes.
        return previous_tokens[0].lower() == later_tokens[0].lower()
    # For structured non-shell tools, retain only stable non-value keys as a
    # bounded relation hint; arbitrary same-name calls are not auto-recovery.
    previous_keys = set(previous.arguments) - {"raw"}
    later_keys = set(later.arguments) - {"raw"}
    return bool(previous_keys and previous_keys == later_keys)


def _react_axis(session: Session, bundle: SessionBundle | None) -> AxisObservation:
    calls = _calls(session)
    known_indices = [index for index, call in enumerate(calls) if _status(call) != "unknown"]
    if not known_indices:
        return AxisObservation("REACT", _not_applicable("no tool outcome was known", excluded=len(calls)), {"total_calls": len(calls), "known_calls": 0, "recovered_failures": 0}, {"recovery_judgment": None}, _event_refs(bundle))
    recovered = 0
    recovery_successes: set[int] = set()
    for index in known_indices:
        call = calls[index]
        if _status(call) != "failed":
            continue
        for later_index, later in enumerate(calls[index + 1:], index + 1):
            if _status(later) == "success" and _related_retry(call, later):
                if later_index not in recovery_successes:
                    recovered += 1
                    recovery_successes.add(later_index)
                break
    successful = sum(1 for call in calls if _status(call) == "success")
    # Success rate and recovery episode count are separate observations.  A
    # recovered failure remains a failed call in this denominator and cannot
    # be counted a second time as a success.
    numerator = successful
    denominator = len(known_indices)
    metric = _ratio(numerator, denominator, excluded=len(calls) - denominator, reason="successful outcomes / known outcomes; recovery episodes reported separately")
    return AxisObservation(
        "REACT",
        metric,
        {"total_calls": len(calls), "known_calls": denominator, "successful_calls": successful, "failed_calls": denominator - successful, "recovered_failures": recovered, "recovery_episodes": recovered},
        {"recovery_judgment": None, "method": "outcome_rate_plus_related_changed_retry_episodes"},
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
            command = _command_text(call).lower()
            name_signal = name in {"pytest", "py.test", "unittest", "test", "verify", "check"}
            command_tokens = re.findall(r"[a-zA-Z0-9_.-]+", command)
            command_signal = False
            if command_tokens:
                command_signal = command_tokens[0] in {"pytest", "py.test", "unittest", "ctest"}
                command_signal = command_signal or (
                    len(command_tokens) >= 2
                    and command_tokens[0] in {"python", "python3", "make", "cargo", "go", "npm", "pnpm", "yarn"}
                    and (
                        command_tokens[1] == "test"
                        or (command_tokens[1] == "-m" and len(command_tokens) > 2 and command_tokens[2] in {"pytest", "unittest"})
                    )
                )
            if name_signal or command_signal:
                found = True
                signal_kinds.append(call.name or command)
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
    if session.task_started_count <= 0 and session.task_complete_count <= 0:
        return AxisObservation(
            "CONV",
            _not_applicable("no explicit task lifecycle start was observed"),
            {"task_started": session.task_started_count, "task_complete": session.task_complete_count, "aborts": session.turn_aborted_count},
            {"delivery_judgment": None, "task_success": None},
            _event_refs(bundle),
            ["Absence of abort is not evidence of delivery completion."],
        )
    if session.task_started_count <= 0:
        return AxisObservation(
            "CONV",
            _unknown("task completion was emitted without a matching task start", excluded=session.task_complete_count),
            {"task_started": 0, "task_complete": session.task_complete_count, "aborts": session.turn_aborted_count, "delivery_units": None},
            {"delivery_judgment": None, "task_success": None, "method": "unpaired_lifecycle_markers"},
            _event_refs(bundle),
            ["A raw completion marker without a matching start is not delivery proof."],
        )
    # Some producers emit task_started once per turn and task_complete once for
    # the session.  Without explicit task identity (or an adapter capability
    # that defines the lifecycle unit), raw counts cannot establish a delivery
    # pair.  Keep that observation unknown instead of turning a lone completion
    # marker into delivery success.
    task_refs: set[str] = set()
    complete_refs: set[str] = set()
    for payload in _lifecycle_events(session, bundle):
        event_type = str(payload.get("type", ""))
        ref = payload.get("task_id", payload.get("taskId", payload.get("task_ref")))
        if event_type == "task_started" and ref:
            task_refs.add(str(ref))
        elif event_type == "task_complete" and ref:
            complete_refs.add(str(ref))
    if not task_refs:
        return AxisObservation(
            "CONV",
            _unknown(
                "lifecycle markers were observed without explicit task identity or pairing capability",
                excluded=session.task_started_count + session.task_complete_count,
            ),
            {
                "task_started": session.task_started_count,
                "task_complete": session.task_complete_count,
                "aborts": session.turn_aborted_count,
                "delivery_units": None,
                "completed_units": None,
                "raw_start_markers": session.task_started_count,
            },
            {"delivery_judgment": None, "task_success": None, "method": "unpaired_lifecycle_markers"},
            _event_refs(bundle),
            ["Raw lifecycle counts are not treated as delivery success without explicit identity/pairing evidence."],
        )
    delivery_units = len(task_refs)
    completed_units = len(task_refs & complete_refs)
    method = "explicit_task_identity_pairs"
    metric = _ratio(completed_units, delivery_units, reason="paired delivery completion units / explicit delivery units")
    return AxisObservation(
        "CONV",
        metric,
        {"task_started": session.task_started_count, "task_complete": session.task_complete_count, "aborts": session.turn_aborted_count, "delivery_units": delivery_units, "completed_units": completed_units, "raw_start_markers": session.task_started_count},
        {"delivery_judgment": None, "task_success": None, "method": method},
        _event_refs(bundle),
        ["Turn lifecycle markers are not assumed to be delivery units; lifecycle completion is separate from external correctness."],
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
    bundle_artifact_id = (
        str(session.manifest.get("artifact_id"))
        if isinstance(session, SessionBundle) and session.manifest.get("artifact_id")
        else None
    )
    candidates = list(artifact) if isinstance(artifact, (list, tuple)) else [artifact]
    source = str(normalized.source) if normalized.source else None
    source_ref = str(normalized.source_ref) if normalized.source_ref else None

    def comparable_ref(value: Any) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return Path(value.replace("\\", "/").split("#", 1)[0]).name or None

    def identity_aliases(value: Mapping[str, Any], aliases: Sequence[str]) -> tuple[str | None, bool]:
        supplied = {
            str(value[alias])
            for alias in aliases
            if alias in value and value[alias] not in (None, "")
        }
        if len(supplied) > 1:
            return None, True
        return (next(iter(supplied)) if supplied else None), False

    metadata = normalized.metadata if isinstance(normalized.metadata, Mapping) else {}
    session_values = [str(normalized.id)] if normalized.id else []
    for alias in ("session_id", "sessionId"):
        if metadata.get(alias) not in (None, ""):
            session_values.append(str(metadata[alias]))
    if len(set(session_values)) > 1:
        return {
            "status": "not_joined",
            "identity_match": False,
            "session_id": None,
            "task_id": None,
            "verdict": None,
            "reason": "session identity fields are contradictory",
        }
    task_values = [
        str(metadata[alias])
        for alias in ("task_id", "taskId")
        if metadata.get(alias) not in (None, "")
    ]
    if len(set(task_values)) > 1:
        return {
            "status": "not_joined",
            "identity_match": False,
            "session_id": session_values[0] if session_values else None,
            "task_id": None,
            "verdict": None,
            "reason": "session task identity fields are contradictory",
        }
    session_id = session_values[0] if session_values else None
    task_id = task_values[0] if task_values else None

    matches: List[Mapping[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        candidate_session_id, session_alias_conflict = identity_aliases(candidate, ("session_id", "sessionId"))
        candidate_task_id, task_alias_conflict = identity_aliases(candidate, ("task_id", "taskId"))
        if session_alias_conflict or task_alias_conflict:
            continue
        supplied = [(candidate_session_id, session_id), (candidate_task_id, task_id)]
        if not any(left is not None and right is not None for left, right in supplied):
            continue
        # Every identity supplied by the fixture must be verifiable and agree;
        # OR matching would attach sess-2/task-A evidence to sess-1/task-A.
        if any(left is not None and (right is None or left != right) for left, right in supplied):
            continue
        candidate_source = candidate.get("source")
        if candidate_source is not None and (source is None or str(candidate_source) != source):
            continue
        candidate_ref, ref_alias_conflict = identity_aliases(candidate, ("source_ref", "sourceRef"))
        if ref_alias_conflict:
            continue
        if candidate_ref is not None:
            if source_ref is None or comparable_ref(candidate_ref) != comparable_ref(source_ref):
                continue
        candidate_artifact_id = candidate.get("artifact_id")
        if candidate_artifact_id is not None:
            if bundle_artifact_id is None or str(candidate_artifact_id) != bundle_artifact_id:
                continue
        matches.append(candidate)
    if len(matches) == 1:
        candidate = matches[0]
        candidate_ref, _ = identity_aliases(candidate, ("source_ref", "sourceRef"))
        source_refs = candidate.get("source_refs", candidate.get("refs", [])) or []
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) for ref in source_refs):
            return {
                "status": "not_joined",
                "identity_match": False,
                "session_id": session_id,
                "task_id": task_id,
                "verdict": None,
                "reason": "external source_refs must be a list of strings",
            }
        return {
            "status": "matched",
            "identity_match": True,
            "session_id": session_id,
            "task_id": task_id,
            "verdict": candidate.get("verdict"),
            "observed_at": candidate.get("observed_at", candidate.get("timestamp")),
            "version": candidate.get("version"),
            "authority": candidate.get("authority"),
            "source": candidate.get("source", source),
            "source_ref": candidate_ref,
            "source_refs": list(source_refs),
            "interpretation": "external_verdict_only; not an internally established correctness claim",
        }
    if len(matches) > 1:
        reason = "multiple external outcomes share the supplied identity"
    else:
        reason = "no exact, non-contradictory identity match"
    return {
        "status": "not_joined",
        "identity_match": False,
        "session_id": session_id,
        "task_id": task_id,
        "verdict": None,
        "reason": reason,
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
