"""Structured report wrapper types for multi-layer session analysis.

These wrappers intentionally sit above the existing quantitative metrics,
PM1/Atlas diagnosis, and agent synthesis layers. They let the project
add report/document metadata without changing the core diagnostic taxonomies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .agent_analysis import AgentAnalysis
from .parser_base import Session
from .scorer import SessionScore


@dataclass
class DiagnosisSummary:
    """Weighted diagnosis view combining quantitative and ProblemMap signals."""

    status: str = ""
    summary_version: str = "weighted-v1"
    integration_mode: str = "weighted-coupling"
    scope: str = "single_session"
    summary_zh: str = ""
    pm_field_guide: List[Dict[str, Any]] = field(default_factory=list)
    pm_candidates: List[Dict[str, Any]] = field(default_factory=list)
    fx_weights: List[Dict[str, Any]] = field(default_factory=list)
    quantitative_summary: Dict[str, Any] = field(default_factory=dict)
    weighted_dimensions: List[Dict[str, Any]] = field(default_factory=list)
    route_summary: Dict[str, Any] = field(default_factory=dict)
    supporting_evidence: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)


@dataclass
class ProblemMapDiagnosis:
    """Structured wrapper for ProblemMap / Atlas diagnosis output."""

    status: str = ""
    diagnostic_mode: str = ""
    pm1_candidates: List[Dict[str, Any]] = field(default_factory=list)
    fx_weights: List[Dict[str, Any]] = field(default_factory=list)
    atlas: Dict[str, Any] = field(default_factory=dict)
    global_fix_route: Dict[str, Any] = field(default_factory=dict)
    references_used: List[str] = field(default_factory=list)
    source_case: str = ""
    need_more_evidence: bool = False

    @property
    def has_route(self) -> bool:
        """Return whether this diagnosis produced a usable Atlas route."""

        primary = str(self.atlas.get("primary_family", "")).strip().lower()
        return bool(primary and primary != "unresolved")


@dataclass
class SessionReport:
    """Wrapper around all report layers for a single session."""

    session: Session
    score: SessionScore
    target_kind: str = "session_file"
    problemmap: Optional[ProblemMapDiagnosis] = None
    diagnosis_summary: Optional[DiagnosisSummary] = None
    agent_analysis: Optional[AgentAnalysis] = None
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    artifact_sources: Dict[str, str] = field(default_factory=dict)
    analysis_layers: List[str] = field(default_factory=list)
    sync_status: str = "session-only"

    def __post_init__(self) -> None:
        if not self.analysis_layers:
            self.analysis_layers = ["quantitative"]
            if self.problemmap is not None:
                self.analysis_layers.append("problemmap")
            if self.diagnosis_summary is not None:
                self.analysis_layers.append("diagnosis")
            if self.agent_analysis is not None and self.agent_analysis.success:
                self.analysis_layers.append("agent")

    @property
    def report_kind(self) -> str:
        """Return the wrapper report type."""

        return "single_session"


@dataclass
class BatchReport:
    """Wrapper around a collection of single-session reports."""

    sessions: List[SessionReport] = field(default_factory=list)
    target_kind: str = "sessions_dir"
    diagnosis_summary: Optional[DiagnosisSummary] = None
    agent_analysis: Optional[AgentAnalysis] = None
    evidence_summary: Dict[str, Any] = field(default_factory=dict)
    artifact_sources: Dict[str, str] = field(default_factory=dict)
    analysis_layers: List[str] = field(default_factory=list)
    sync_status: str = "session-only"

    def __post_init__(self) -> None:
        if not self.analysis_layers:
            layers = {"quantitative"}
            for report in self.sessions:
                layers.update(report.analysis_layers)
            if self.diagnosis_summary is not None:
                layers.add("diagnosis")
            if self.agent_analysis is not None and self.agent_analysis.success:
                layers.add("agent")
            self.analysis_layers = sorted(layers)

    @property
    def report_kind(self) -> str:
        """Return the wrapper report type."""

        return "batch"
