"""Portable, bounded SessionBundle support for offline analysis.

The bundle is deliberately a small JSON contract above the source adapters.  It
contains normalized events and facts rather than an opaque copy of a session
log.  All source paths are reduced to relative artifact references and evidence
text is redacted and bounded before it is serialized.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .parser_base import Session, ToolCall, Turn


BUNDLE_SCHEMA = "session-health.session-bundle"
BUNDLE_VERSION = "1.0"
PARSER_CONTRACT_VERSION = "canonical-events-1"


class BundleError(ValueError):
    """Raised when a bundle is malformed or exceeds a configured limit."""


class UnsupportedBundleVersion(BundleError):
    """Raised when the bundle major version is newer than this reader."""


@dataclass(frozen=True)
class BundleLimits:
    """Hard limits applied while creating or reading a bundle."""

    max_events: int = 10_000
    max_bytes: int = 2_000_000
    max_evidence_chars: int = 2_000
    max_cases: int = 1_000


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\b(?:api[_-]?key|token|secret|password)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
)


def redact_text(value: Any, max_chars: int = 2_000) -> tuple[str, Dict[str, int]]:
    """Return bounded text with common credential material replaced.

    This is an allowlist-independent safety layer, not a claim that arbitrary
    secrets can be detected.  The returned counters make redaction observable.
    """

    text = "" if value is None else str(value)
    counters = {"patterns": 0, "truncated_chars": 0}
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn(
            lambda match: "[REDACTED]" if match.lastindex is None else match.group(1) + "[REDACTED]",
            text,
        )
        counters["patterns"] += count
    if len(text) > max_chars:
        counters["truncated_chars"] = len(text) - max_chars
        text = text[:max_chars] + "…[TRUNCATED]"
    return text, counters


def _redact_value(value: Any, max_chars: int, counters: Dict[str, int]) -> Any:
    if isinstance(value, str):
        redacted, local = redact_text(value, max_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        return redacted
    if isinstance(value, dict):
        return {
            str(key): _redact_value(item, max_chars, counters)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_value(item, max_chars, counters) for item in value]
    if isinstance(value, tuple):
        return [_redact_value(item, max_chars, counters) for item in value]
    return value


def _relative_source_ref(source_ref: str) -> str:
    if not source_ref:
        return "session.jsonl"
    # Preserve a useful artifact name without making the original machine path
    # a prerequisite for replay.
    return Path(str(source_ref).replace("\\", "/")).name or "session.jsonl"


def _stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts).encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:20]


def _safe_status(call: ToolCall) -> str:
    if call.success is True or call.exit_code == 0:
        return "success"
    if call.success is False or (call.exit_code is not None and call.exit_code != 0):
        return "failed"
    return "unknown"


def _canonical_session(session: Session, counters: Dict[str, int]) -> Dict[str, Any]:
    """Serialize the minimum session model needed to replay offline."""

    turns: List[Dict[str, Any]] = []
    for turn in session.turns:
        user_input, user_counts = redact_text(turn.user_input)
        assistant_output, assistant_counts = redact_text(turn.assistant_output)
        counters["patterns"] += user_counts["patterns"] + assistant_counts["patterns"]
        counters["truncated_chars"] += user_counts["truncated_chars"] + assistant_counts["truncated_chars"]
        calls = []
        for call in turn.tool_calls:
            arguments = _redact_value(call.arguments, 1_000, counters)
            output, output_counts = redact_text(call.output)
            counters["patterns"] += output_counts["patterns"]
            counters["truncated_chars"] += output_counts["truncated_chars"]
            calls.append(
                {
                    "name": call.name,
                    "arguments": arguments,
                    "call_id": call.call_id,
                    "output": output,
                    "success": call.success,
                    "exit_code": call.exit_code,
                    "status": _safe_status(call),
                    "source_ref": _relative_source_ref(call.source_ref),
                }
            )
        turns.append(
            {
                "index": turn.index,
                "user_input": user_input,
                "assistant_output": assistant_output,
                "tool_calls": calls,
                "events": _redact_value(turn.events, 1_000, counters),
                "context_meta": _redact_value(turn.context_meta, 200, counters),
                "timestamp": turn.timestamp,
                "raw_tool_output_chars": turn.raw_tool_output_chars,
                "total_context_chars": turn.total_context_chars,
                "diagnostics": _redact_value(turn.diagnostics, 500, counters),
            }
        )

    safe_metadata = {}
    for key in ("id", "model", "model_provider", "cli_version", "sessionId", "copilotVersion"):
        if key in session.metadata:
            safe_metadata[key] = _redact_value(session.metadata[key], 300, counters)

    return {
        "id": session.id,
        "source": session.source,
        "model": session.model,
        "cli_version": session.cli_version,
        "turns": turns,
        "metadata": safe_metadata,
        "timestamp_start": session.timestamp_start,
        "timestamp_end": session.timestamp_end,
        "context_compacted_count": session.context_compacted_count,
        "task_started_count": session.task_started_count,
        "task_complete_count": session.task_complete_count,
        "turn_aborted_count": session.turn_aborted_count,
        "diagnostics": _redact_value(session.diagnostics, 500, counters),
        "parser_version": session.parser_version,
        "source_ref": _relative_source_ref(session.source_ref),
    }


def _build_events(session: Session, limits: BundleLimits, counters: Dict[str, int]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    sequence = 0

    def append(kind: str, turn: Optional[Turn], payload: Dict[str, Any], source_ref: str = "") -> None:
        nonlocal sequence
        sequence += 1
        if len(events) >= limits.max_events:
            return
        safe_payload = _redact_value(payload, limits.max_evidence_chars, counters)
        events.append(
            {
                "event_id": _stable_id(session.id, sequence, kind, payload.get("call_id", "")),
                "sequence": sequence,
                "kind": kind,
                "turn_index": turn.index if turn is not None else None,
                "timestamp": turn.timestamp if turn is not None else session.timestamp_end,
                "source_ref": _relative_source_ref(source_ref or session.source_ref),
                "payload": safe_payload,
            }
        )

    for turn in session.turns:
        if turn.user_input:
            append("user_message", turn, {"text": turn.user_input})
        for call in turn.tool_calls:
            append(
                "tool_call",
                turn,
                {
                    "call_id": call.call_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "status": _safe_status(call),
                },
                call.source_ref,
            )
            if call.output or call.success is not None or call.exit_code is not None:
                append(
                    "tool_result",
                    turn,
                    {
                        "call_id": call.call_id,
                        "output": call.output,
                        "success": call.success,
                        "exit_code": call.exit_code,
                        "status": _safe_status(call),
                    },
                    call.source_ref,
                )
        if turn.assistant_output:
            append("assistant_message", turn, {"text": turn.assistant_output})
        for event in turn.events:
            append("session_event", turn, event)
        for diagnostic in turn.diagnostics:
            append("diagnostic", turn, diagnostic)

    for diagnostic in session.diagnostics:
        if diagnostic not in [event["payload"] for event in events if event["kind"] == "diagnostic"]:
            append("diagnostic", None, diagnostic)

    coverage = {
        "status": "complete" if len(events) < limits.max_events else "truncated",
        "input_turns": len(session.turns),
        "exported_events": len(events),
        "max_events": limits.max_events,
        "excluded_events": max(0, sequence - len(events)),
        "observed_cutoff": session.timestamp_end or None,
    }
    return events, coverage


def _build_facts(session: Session, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    calls = [call for turn in session.turns for call in turn.tool_calls]
    known = [call for call in calls if _safe_status(call) != "unknown"]
    return {
        "session_id": session.id,
        "source": session.source,
        "turn_count": len(session.turns),
        "tool_call_count": len(calls),
        "tool_call_outcomes": {
            "success": sum(1 for call in calls if _safe_status(call) == "success"),
            "failed": sum(1 for call in calls if _safe_status(call) == "failed"),
            "unknown": len(calls) - len(known),
        },
        "event_count": len(events),
        "lifecycle": {
            "task_started": session.task_started_count,
            "task_complete": session.task_complete_count,
            "turn_aborted": session.turn_aborted_count,
            "context_compacted": session.context_compacted_count,
        },
        "observed_fields": {
            "cwd": bool(session.cwd),
            "model": bool(session.model),
            "timestamps": bool(session.timestamp_start or session.timestamp_end),
        },
    }


def _build_evidence_and_cases(
    session: Session,
    events: List[Dict[str, Any]],
    limits: BundleLimits,
    counters: Dict[str, int],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    evidence: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    for turn in session.turns:
        if len(cases) >= limits.max_cases:
            break
        turn_events = [event for event in events if event.get("turn_index") == turn.index]
        refs = [event["event_id"] for event in turn_events]
        text_parts = [turn.user_input, turn.assistant_output]
        text_parts.extend(call.output for call in turn.tool_calls if call.output)
        text = "\n".join(part for part in text_parts if part)
        safe_text, local = redact_text(text, limits.max_evidence_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        ref_id = f"evidence-{_stable_id(session.id, turn.index)}"
        evidence.append(
            {
                "ref_id": ref_id,
                "kind": "turn_observation",
                "event_refs": refs,
                "text": safe_text,
                "observation_cutoff": turn.timestamp or session.timestamp_end or None,
                "redaction": {"bounded": True, "patterns": local["patterns"]},
            }
        )
        cases.append(
            {
                "case_id": f"case-{_stable_id(session.id, turn.index)}",
                "kind": "offline_observation_candidate",
                "turn_index": turn.index,
                "evidence_refs": [ref_id],
                "event_refs": refs,
                "relations": {
                    "requirement_to_action": "candidate" if turn.user_input and turn.tool_calls else "insufficient",
                    "failure_to_disposition_to_result": "candidate" if any(_safe_status(call) == "failed" for call in turn.tool_calls) else "not_observed",
                    "claim_to_verification": "candidate" if turn.assistant_output and any("test" in event.get("kind", "") or "verify" in event.get("kind", "") for event in turn_events) else "insufficient",
                },
                "observation_cutoff": turn.timestamp or session.timestamp_end or None,
                "semantic_judgment": "not_requested",
            }
        )
    return evidence, cases


@dataclass
class SessionBundle:
    """Versioned portable offline input and evidence artifact."""

    manifest: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    facts: Dict[str, Any] = field(default_factory=dict)
    source_capabilities: Dict[str, Any] = field(default_factory=dict)
    source_refs: List[str] = field(default_factory=list)
    evidence_refs: List[Dict[str, Any]] = field(default_factory=list)
    cases: List[Dict[str, Any]] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    session: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_session(cls, session: Session, limits: BundleLimits | None = None) -> "SessionBundle":
        limits = limits or BundleLimits()
        counters = {"patterns": 0, "truncated_chars": 0}
        events, coverage = _build_events(session, limits, counters)
        evidence, cases = _build_evidence_and_cases(session, events, limits, counters)
        canonical = _canonical_session(session, counters)
        bundle = cls(
            manifest={
                "schema": BUNDLE_SCHEMA,
                "version": BUNDLE_VERSION,
                "schema_version": BUNDLE_VERSION,
                "bundle_version": BUNDLE_VERSION,
                "session_id": session.id,
                "source": session.source,
                "source_ref": _relative_source_ref(session.source_ref),
                "parser_version": session.parser_version,
                "portable": True,
                "redaction": {
                    "patterns": counters["patterns"],
                    "truncated_chars": counters["truncated_chars"],
                    "secret_detection_disclaimer": "Redaction is bounded heuristic detection, not proof that no secret remains.",
                },
            },
            events=events,
            facts=_build_facts(session, events),
            source_capabilities=dict(session.source_capabilities),
            source_refs=sorted({
                str(event.get("source_ref"))
                for event in events
                if event.get("source_ref")
            }),
            evidence_refs=evidence,
            cases=cases,
            coverage=coverage,
            session=canonical,
            diagnostics=list(session.diagnostics),
        )
        encoded = bundle.to_json()
        if len(encoded.encode("utf-8")) > limits.max_bytes:
            # A deterministic failure is safer than silently emitting a partial
            # artifact whose coverage claims would be wrong.
            raise BundleError(
                f"bundle exceeds max_bytes={limits.max_bytes}; reduce input or raise the explicit limit"
            )
        return bundle

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": BUNDLE_SCHEMA,
            "version": BUNDLE_VERSION,
            "schema_version": BUNDLE_VERSION,
            "manifest": self.manifest,
            "events": self.events,
            "facts": self.facts,
            "source_capabilities": self.source_capabilities,
            "source_refs": self.source_refs,
            "evidence_refs": self.evidence_refs,
            "cases": self.cases,
            "coverage": self.coverage,
            "session": self.session,
            "diagnostics": self.diagnostics,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], limits: BundleLimits | None = None) -> "SessionBundle":
        if not isinstance(payload, Mapping):
            raise BundleError("bundle root must be an object")
        schema = payload.get("schema")
        version = str(payload.get("version", payload.get("schema_version", "")))
        if schema != BUNDLE_SCHEMA:
            raise BundleError(f"unsupported bundle schema: {schema!r}")
        try:
            major = int(version.split(".", 1)[0])
        except (ValueError, AttributeError):
            raise BundleError("bundle version must be major.minor")
        if major != int(BUNDLE_VERSION.split(".", 1)[0]):
            raise UnsupportedBundleVersion(f"unsupported bundle major version: {version}")
        limits = limits or BundleLimits()
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise BundleError("bundle events must be a list")
        if len(events) > limits.max_events:
            raise BundleError("bundle event limit exceeded")
        bundle = cls(
            manifest=dict(payload.get("manifest", {})),
            events=list(events),
            facts=dict(payload.get("facts", {})),
            source_capabilities=dict(payload.get("source_capabilities", {})),
            source_refs=[str(item) for item in payload.get("source_refs", [])],
            evidence_refs=list(payload.get("evidence_refs", [])),
            cases=list(payload.get("cases", [])),
            coverage=dict(payload.get("coverage", {})),
            session=dict(payload.get("session", {})),
            diagnostics=list(payload.get("diagnostics", [])),
        )
        if len(bundle.to_json().encode("utf-8")) > limits.max_bytes:
            raise BundleError("bundle byte limit exceeded")
        return bundle

    @classmethod
    def from_json(cls, text: str, limits: BundleLimits | None = None) -> "SessionBundle":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise BundleError(f"invalid bundle JSON: {exc}") from exc
        return cls.from_dict(payload, limits=limits)

    def to_session(self) -> Session:
        """Reconstruct the normalized Session used by the existing scorer."""

        raw = self.session
        if not raw:
            raise BundleError("bundle does not contain a replayable session")
        session = Session(
            id=str(raw.get("id", self.manifest.get("session_id", ""))),
            source=str(raw.get("source", self.manifest.get("source", "unknown"))),
            model=str(raw.get("model", "")),
            cli_version=str(raw.get("cli_version", "")),
            metadata=dict(raw.get("metadata", {})),
            timestamp_start=str(raw.get("timestamp_start", "")),
            timestamp_end=str(raw.get("timestamp_end", "")),
            context_compacted_count=int(raw.get("context_compacted_count", 0) or 0),
            task_started_count=int(raw.get("task_started_count", 0) or 0),
            task_complete_count=int(raw.get("task_complete_count", 0) or 0),
            turn_aborted_count=int(raw.get("turn_aborted_count", 0) or 0),
            diagnostics=list(raw.get("diagnostics", self.diagnostics)),
            parser_version=str(raw.get("parser_version", "bundle-1")),
            source_ref=str(raw.get("source_ref", self.manifest.get("source_ref", "session.jsonl"))),
            source_capabilities=dict(self.source_capabilities),
        )
        for raw_turn in raw.get("turns", []):
            turn = Turn(
                index=int(raw_turn.get("index", len(session.turns) + 1)),
                user_input=str(raw_turn.get("user_input", "")),
                assistant_output=str(raw_turn.get("assistant_output", "")),
                events=list(raw_turn.get("events", [])),
                context_meta=dict(raw_turn.get("context_meta", {})),
                timestamp=str(raw_turn.get("timestamp", "")),
                raw_tool_output_chars=int(raw_turn.get("raw_tool_output_chars", 0) or 0),
                total_context_chars=int(raw_turn.get("total_context_chars", 0) or 0),
                diagnostics=list(raw_turn.get("diagnostics", [])),
            )
            for raw_call in raw_turn.get("tool_calls", []):
                call = ToolCall(
                    name=str(raw_call.get("name", "unknown")),
                    arguments=dict(raw_call.get("arguments", {})) if isinstance(raw_call.get("arguments", {}), dict) else {"raw": raw_call.get("arguments")},
                    call_id=str(raw_call.get("call_id", "")),
                    output=str(raw_call.get("output", "")),
                    success=raw_call.get("success") if isinstance(raw_call.get("success"), bool) else None,
                    exit_code=raw_call.get("exit_code") if isinstance(raw_call.get("exit_code"), int) else None,
                    status=str(raw_call.get("status", "unknown")),
                    source_ref=str(raw_call.get("source_ref", "")),
                )
                turn.tool_calls.append(call)
            session.turns.append(turn)
        return session


def build_session_bundle(session: Session, limits: BundleLimits | None = None) -> SessionBundle:
    return SessionBundle.from_session(session, limits=limits)


def export_bundle(session_or_bundle: Session | SessionBundle, path: str | Path, limits: BundleLimits | None = None) -> Path:
    bundle = session_or_bundle if isinstance(session_or_bundle, SessionBundle) else build_session_bundle(session_or_bundle, limits=limits)
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(bundle.to_json() + "\n", encoding="utf-8")
    return destination


def import_bundle(path: str | Path, limits: BundleLimits | None = None) -> SessionBundle:
    source = Path(path)
    limits = limits or BundleLimits()
    if source.stat().st_size > limits.max_bytes:
        raise BundleError("bundle byte limit exceeded")
    return SessionBundle.from_json(source.read_text(encoding="utf-8"), limits=limits)


load_bundle = import_bundle
session_from_bundle = lambda bundle: bundle.to_session() if isinstance(bundle, SessionBundle) else SessionBundle.from_dict(bundle).to_session()
