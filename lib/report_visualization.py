"""Load already-rendered report JSON without re-running session analysis.

The command line evaluator writes a portable report projection.  This module
turns that projection back into the small typed view consumed by the HTML
renderer.  It deliberately does not parse session logs, call metrics, or
invoke an analyzer.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
from pathlib import Path
from typing import Any

from .agent_analysis import AgentAnalysis
from .jev_analysis import SemanticEvaluation
from .metrics.process_v2 import (
    AXIS_IDS,
    AxisObservation,
    ObservableMetric,
    ProcessV2Result,
)
from .parser_base import Session
from .report_types import BatchReport, DiagnosisSummary, ProblemMapDiagnosis, SessionReport
from .scorer import SessionScore


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _number(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _integer(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _diagnosis_summary(value: Any) -> DiagnosisSummary | None:
    data = _mapping(value)
    if not data:
        return None
    fields = {
        "status",
        "summary_version",
        "integration_mode",
        "scope",
        "summary_zh",
        "pm_field_guide",
        "pm_candidates",
        "fx_weights",
        "quantitative_summary",
        "weighted_dimensions",
        "route_summary",
        "supporting_evidence",
        "notes",
    }
    kwargs = {key: data[key] for key in fields if key in data}
    for key in ("pm_field_guide", "pm_candidates", "fx_weights", "weighted_dimensions"):
        if key in kwargs and not isinstance(kwargs[key], list):
            kwargs[key] = []
    for key in ("quantitative_summary", "route_summary", "supporting_evidence"):
        if key in kwargs and not isinstance(kwargs[key], Mapping):
            kwargs[key] = {}
    if "notes" in kwargs and not isinstance(kwargs["notes"], list):
        kwargs["notes"] = []
    return DiagnosisSummary(**kwargs)


def _problemmap(value: Any) -> ProblemMapDiagnosis | None:
    data = _mapping(value)
    if not data:
        return None
    fields = {
        "status",
        "diagnostic_mode",
        "pm1_candidates",
        "fx_weights",
        "atlas",
        "global_fix_route",
        "references_used",
        "source_case",
        "need_more_evidence",
    }
    kwargs = {key: data[key] for key in fields if key in data}
    for key in ("pm1_candidates", "fx_weights", "references_used"):
        if key in kwargs and not isinstance(kwargs[key], list):
            kwargs[key] = []
    for key in ("atlas", "global_fix_route"):
        if key in kwargs and not isinstance(kwargs[key], Mapping):
            kwargs[key] = {}
    return ProblemMapDiagnosis(**kwargs)


def _process_result(value: Any) -> ProcessV2Result | None:
    data = _mapping(value)
    if not data:
        return None
    axes: dict[str, AxisObservation] = {}
    raw_axes = _mapping(data.get("axes"))
    for axis_id in AXIS_IDS:
        raw_axis = _mapping(raw_axes.get(axis_id))
        if not raw_axis:
            continue
        raw_metric = _mapping(raw_axis.get("metric"))
        metric = ObservableMetric(
            numerator=raw_metric.get("numerator"),
            denominator=raw_metric.get("denominator"),
            excluded_count=_integer(raw_metric.get("excluded_count")),
            value=raw_metric.get("value"),
            coverage=raw_metric.get("coverage"),
            applicability=str(raw_metric.get("applicability", "unknown")),
            status=str(raw_metric.get("status", "unknown")),
            reason=str(raw_metric.get("reason", "")),
        )
        axes[axis_id] = AxisObservation(
            axis_id=str(raw_axis.get("axis_id", axis_id)),
            metric=metric,
            observed_facts=dict(_mapping(raw_axis.get("observed_facts"))),
            inference=dict(_mapping(raw_axis.get("inference"))),
            evidence_refs=[str(item) for item in raw_axis.get("evidence_refs", []) if item is not None]
            if isinstance(raw_axis.get("evidence_refs"), list)
            else [],
            notes=[str(item) for item in raw_axis.get("notes", []) if item is not None]
            if isinstance(raw_axis.get("notes"), list)
            else [],
        )
    return ProcessV2Result(
        profile=str(data.get("profile", "process-v2")),
        version=str(data.get("version", "process-v2.1")),
        status=str(data.get("status", "unknown")),
        axes=axes,
        observed_facts=dict(_mapping(data.get("observed_facts"))),
        inference=dict(_mapping(data.get("inference"))),
        external_outcome=dict(_mapping(data.get("external_outcome"))),
        coverage=dict(_mapping(data.get("coverage"))),
        processing=dict(_mapping(data.get("processing"))),
    )


def _agent_analysis(value: Any) -> AgentAnalysis | None:
    data = _mapping(value)
    if not data:
        return None
    # Routing is retained as JSON in saved reports.  The existing HTML section
    # only needs it for metadata, so leave it absent rather than reconstructing
    # an authority-bearing RouteDecision from a projection.
    return AgentAnalysis(
        agent_name=str(data.get("agent_name", "saved-report")),
        raw_response=str(data.get("raw_response", "")),
        success=bool(data.get("success")),
        error=str(data.get("error", "")),
        requested_model=data.get("requested_model"),
        actual_model=data.get("actual_model"),
        requested_settings=dict(_mapping(data.get("requested_settings"))),
        actual_settings=dict(_mapping(data.get("actual_settings"))),
        native_usage=dict(_mapping(data.get("native_usage"))),
        usage_scope=str(data.get("usage_scope", "analyzer_invocation_native")),
        structured_output=dict(_mapping(data.get("structured_output"))),
        claims=_list_of_dicts(data.get("claims")),
        recommendations=_list_of_dicts(data.get("recommendations")),
        attempts=_list_of_dicts(data.get("attempts")),
        repair_attempts=_list_of_dicts(data.get("repair_attempts")),
        postcheck=data.get("postcheck"),
        coverage=dict(_mapping(data.get("coverage"))),
        diagnostics=_list_of_dicts(data.get("diagnostics")),
    )


def _semantic(value: Any) -> SemanticEvaluation | None:
    data = _mapping(value)
    if not data:
        return None
    fields = {
        "version",
        "status",
        "live_status",
        "backend",
        "capabilities",
        "state",
        "cases",
        "questions",
        "answers",
        "raw_judgments",
        "stages",
        "coverage",
        "diagnostics",
        "usage",
        "ledger",
        "provenance",
    }
    return SemanticEvaluation(**{key: data[key] for key in fields if key in data})


def _score(value: Mapping[str, Any]) -> SessionScore:
    dimensions = _mapping(value.get("dimensions"))
    events = _mapping(value.get("events"))
    stats = _mapping(value.get("stats"))
    return SessionScore(
        session_id=str(value.get("session_id", "")),
        source=str(value.get("source", "unknown")),
        model=str(value.get("model", "unknown")),
        turn_count=_integer(value.get("turn_count")),
        snr=_number(dimensions.get("SNR")),
        state=_number(dimensions.get("STATE")),
        context=_number(dimensions.get("CTX")),
        reaction=_number(dimensions.get("REACT")),
        depth=_number(dimensions.get("DEPTH")),
        convergence=_number(dimensions.get("CONV")),
        tool_efficiency=_number(dimensions.get("TOOL")),
        composite=_number(value.get("composite")),
        composite_min=_number(stats.get("min")),
        composite_max=_number(stats.get("max")),
        composite_stddev=_number(stats.get("stddev")),
        compaction_count=_integer(events.get("compactions")),
        abort_count=_integer(events.get("aborts")),
    )


def session_report_from_saved(value: Mapping[str, Any], *, target_kind: str = "session_file") -> SessionReport:
    """Restore one report projection for rendering only."""

    payload = dict(value)
    score = _score(payload)
    process_result = _process_result(payload.get("process_v2"))
    profile = str(payload.get("profile") or ("process-v2" if process_result is not None else "legacy"))
    session = Session(
        id=score.session_id,
        source=score.source,
        model=score.model,
    )
    return SessionReport(
        session=session,
        score=score,
        target_kind=str(payload.get("target_kind", target_kind)),
        problemmap=_problemmap(payload.get("problemmap")),
        diagnosis_summary=_diagnosis_summary(payload.get("diagnosis_summary")),
        agent_analysis=_agent_analysis(payload.get("agent_analysis")),
        evidence_summary=dict(_mapping(payload.get("evidence_summary"))),
        artifact_sources={str(key): str(item) for key, item in _mapping(payload.get("artifact_sources")).items()},
        analysis_layers=[str(item) for item in payload.get("analysis_layers", [])]
        if isinstance(payload.get("analysis_layers"), list)
        else [],
        sync_status=str(payload.get("sync_status", "session-only")),
        profile=profile,
        process_v2=process_result,
        bundle_manifest=dict(_mapping(payload.get("bundle"))),
        processing_status=str(payload.get("processing_status", "complete")),
        processing_diagnostics=_list_of_dicts(payload.get("processing_diagnostics")),
        analysis_status=str(payload.get("analysis_status", "not_requested")),
        analysis_coverage=dict(_mapping(payload.get("analysis_coverage"))),
        routing=payload.get("routing"),
        postcheck=payload.get("postcheck"),
        semantic=_semantic(payload.get("semantic")),
    )


def _infer_profile(sessions: Sequence[Mapping[str, Any]], explicit: Any = None) -> str:
    if explicit:
        return str(explicit)
    profiles = {
        str(item.get("profile") or ("process-v2" if item.get("process_v2") is not None else "legacy"))
        for item in sessions
    }
    if len(profiles) > 1:
        raise ValueError("saved JSON contains mixed report profiles; render one profile at a time")
    return next(iter(profiles), "legacy")


def batch_report_from_saved(value: Mapping[str, Any], *, source_name: str = "saved JSON") -> BatchReport:
    """Restore a batch report projection without recomputing its sessions."""

    payload = dict(value)
    raw_sessions = payload.get("sessions", [])
    if not isinstance(raw_sessions, list):
        raise ValueError("saved batch JSON must contain a sessions list")
    sessions = [
        session_report_from_saved(item, target_kind=str(payload.get("target_kind", "sessions_dir")))
        for item in raw_sessions
        if isinstance(item, Mapping)
    ]
    return BatchReport(
        sessions=sessions,
        target_kind=str(payload.get("target_kind", "sessions_dir")),
        diagnosis_summary=_diagnosis_summary(payload.get("diagnosis_summary")),
        agent_analysis=_agent_analysis(payload.get("agent_analysis")),
        evidence_summary=dict(_mapping(payload.get("evidence_summary"))),
        artifact_sources={str(key): str(item) for key, item in _mapping(payload.get("artifact_sources")).items()},
        analysis_layers=[str(item) for item in payload.get("analysis_layers", [])]
        if isinstance(payload.get("analysis_layers"), list)
        else [],
        sync_status=str(payload.get("sync_status", "session-only")),
        profile=_infer_profile([item for item in raw_sessions if isinstance(item, Mapping)], payload.get("profile")),
        processing_status=str(payload.get("processing_status", "complete")),
        processing_diagnostics=_list_of_dicts(payload.get("processing_diagnostics")),
        analysis_status=str(payload.get("analysis_status", "not_requested")),
        analysis_coverage=dict(_mapping(payload.get("analysis_coverage"))),
        routing=payload.get("routing"),
        postcheck=payload.get("postcheck"),
        semantic=_semantic(payload.get("semantic")),
    )


def report_from_saved_payload(value: Any, *, source_name: str = "saved JSON") -> SessionReport | BatchReport:
    """Classify and restore a single or batch report JSON projection."""

    if not isinstance(value, Mapping):
        raise ValueError(f"{source_name} must contain a JSON object")
    if value.get("report_kind") == "batch" or isinstance(value.get("sessions"), list):
        return batch_report_from_saved(value, source_name=source_name)
    return session_report_from_saved(value)


def load_saved_report(path: str | Path, *, input_kind: str = "auto") -> SessionReport | BatchReport:
    """Load a saved report file or directory of per-session JSON files."""

    source = Path(path)
    if input_kind not in {"auto", "single", "batch", "directory"}:
        raise ValueError("input_kind must be auto, single, batch, or directory")
    is_directory = source.is_dir()
    if input_kind == "directory" or (input_kind == "auto" and is_directory):
        if not is_directory:
            raise ValueError(f"directory input does not exist: {source}")
        files = sorted(item for item in source.glob("*.json") if item.is_file())
        if not files:
            raise ValueError(f"directory contains no .json session reports: {source}")
        sessions: list[dict[str, Any]] = []
        for item in files:
            with item.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            if not isinstance(payload, Mapping) or payload.get("report_kind") == "batch" or isinstance(payload.get("sessions"), list):
                raise ValueError(f"directory input requires per-session JSON; found batch payload in {item.name}")
            sessions.append(dict(payload))
        profile = _infer_profile(sessions)
        return batch_report_from_saved(
            {
                "report_kind": "batch",
                "target_kind": "saved_report_directory",
                "profile": profile,
                "sessions": sessions,
                "artifact_sources": {"saved_input": source.name or "saved-reports"},
            },
            source_name=str(source),
        )
    if is_directory:
        raise ValueError(f"input_kind={input_kind} requires a JSON file, not a directory")
    if not source.is_file():
        raise ValueError(f"saved report does not exist: {source}")
    with source.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    report = report_from_saved_payload(payload, source_name=str(source))
    if input_kind == "single" and isinstance(report, BatchReport):
        raise ValueError("input_kind=single received a batch JSON")
    if input_kind == "batch" and isinstance(report, SessionReport):
        raise ValueError("input_kind=batch received a single-session JSON")
    return report
