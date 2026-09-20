"""Portable, bounded SessionBundle support for offline analysis.

The bundle is a versioned, typed projection of a parsed session.  It retains
ordered evidence and bounded facts, but never transports raw system/developer
payloads or arbitrary unknown event bodies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Mapping, Optional

from .parser_base import Session, ToolCall, Turn


BUNDLE_SCHEMA = "session-health.session-bundle"
BUNDLE_VERSION = "1.1"
PARSER_CONTRACT_VERSION = "canonical-events-2"


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
    max_turns: int = 10_000
    max_tool_calls: int = 20_000


_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)(\b(?:api[_-]?key|token|secret|password|authorization)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"-----BEGIN [^-]+-----.*?-----END [^-]+-----", re.DOTALL),
)
_ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_:/])/(?:[A-Za-z0-9._-]+/)+[A-Za-z0-9._-]+"
)
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "access_key",
    "accesskey",
    "authorization",
    "credential",
    "credentials",
    "password",
    "private_key",
    "privatekey",
    "secret",
    "token",
}
_IDENTITY_KEYS = {
    "id",
    "sessionid",
    "session_id",
    "taskid",
    "task_id",
    "model",
    "model_provider",
    "cliversion",
    "cli_version",
    "copilotversion",
    "copilotVersion",
}


def _key_name(key: Any) -> str:
    return str(key).replace("-", "_").lower()


def redact_text(value: Any, max_chars: int = 2_000) -> tuple[str, Dict[str, int]]:
    """Return bounded text with common credential material replaced."""

    text = "" if value is None else str(value)
    counters = {"patterns": 0, "truncated_chars": 0}
    for pattern in _SECRET_PATTERNS:
        text, count = pattern.subn(
            lambda match: "[REDACTED]"
            if match.lastindex is None
            else match.group(1) + "[REDACTED]",
            text,
        )
        counters["patterns"] += count
    text, path_count = _ABSOLUTE_PATH_PATTERN.subn("<absolute-path>", text)
    if path_count:
        counters["absolute_paths"] = counters.get("absolute_paths", 0) + path_count
    if len(text) > max_chars:
        counters["truncated_chars"] = len(text) - max_chars
        text = text[:max_chars] + "…[TRUNCATED]"
    return text, counters


def _redact_value(value: Any, max_chars: int, counters: Dict[str, int], key: Any = "") -> Any:
    """Recursively redact values, including structured sensitive keys."""

    if _key_name(key) in _SENSITIVE_KEYS:
        counters["patterns"] += 1
        return "[REDACTED]"
    if isinstance(value, str):
        redacted, local = redact_text(value, max_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        return redacted
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_value(item, max_chars, counters, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, max_chars, counters) for item in value]
    return value


def _portable_cwd(value: Any, counters: Dict[str, int]) -> str:
    if not value:
        return ""
    text = str(value).replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:/", text):
        return "<absolute-path>"
    safe, local = redact_text(text, 300)
    counters["patterns"] += local["patterns"]
    counters["truncated_chars"] += local["truncated_chars"]
    return safe


def _relative_source_ref(source_ref: str) -> str:
    """Keep only a relative artifact name while preserving ``#L<line>``."""

    if not source_ref:
        return "session.jsonl"
    raw = str(source_ref).replace("\\", "/")
    path_part, marker, suffix = raw.partition("#")
    name = Path(path_part).name or "session.jsonl"
    return f"{name}#{suffix}" if marker and suffix else name


def _stable_id(*parts: Any) -> str:
    payload = "|".join(str(part) for part in parts).encode("utf-8", "replace")
    return hashlib.sha256(payload).hexdigest()[:20]


def _safe_status(call: ToolCall) -> str:
    if call.success is True or call.exit_code == 0:
        return "success"
    if call.success is False or (call.exit_code is not None and call.exit_code != 0):
        return "failed"
    return "unknown"


def _observed_output_chars(turn: Turn) -> int:
    visible = sum(len(call.output or "") for call in turn.tool_calls)
    return max(int(turn.raw_tool_output_chars or 0), visible)


def _safe_turn_event(event: Any, counters: Dict[str, int]) -> Dict[str, Any]:
    """Retain typed lifecycle fields while excluding raw unknown payloads."""

    if not isinstance(event, Mapping):
        return {"type": "unknown", "status": "unknown"}
    event_type = str(event.get("type", "unknown") or "unknown")
    result: Dict[str, Any] = {"type": event_type}
    for key in (
        "status",
        "task_ref",
        "task_id",
        "taskId",
        "session_ref",
        "session_id",
        "sessionId",
        "reason",
        "exit_code",
        "exitCode",
    ):
        if key in event:
            result[key] = _redact_value(event[key], 300, counters, key)
    return result


def _safe_event_payload(kind: str, payload: Any, limits: BundleLimits, counters: Dict[str, int]) -> Dict[str, Any]:
    """Allowlist canonical event fields before applying recursive redaction."""

    if not isinstance(payload, Mapping):
        return {"type": "unknown", "status": "unknown"}
    if kind in {"user_message", "assistant_message"}:
        text, local = redact_text(payload.get("text", ""), limits.max_evidence_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        return {"text": text}
    if kind == "tool_call":
        return _redact_value(
            {
                "call_id": payload.get("call_id", ""),
                "name": payload.get("name", "unknown"),
                "arguments": payload.get("arguments", {}),
                "status": payload.get("status", "unknown"),
            },
            limits.max_evidence_chars,
            counters,
        )
    if kind == "tool_result":
        output, local = redact_text(payload.get("output", ""), limits.max_evidence_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        safe = {
            "call_id": payload.get("call_id", ""),
            "output": output,
            "success": payload.get("success") if isinstance(payload.get("success"), bool) else None,
            "exit_code": payload.get("exit_code") if isinstance(payload.get("exit_code"), int) else None,
            "result_metadata": payload.get("result_metadata", {}),
        }
        return _redact_value(safe, limits.max_evidence_chars, counters)
    if kind in {"session_event", "context_metadata", "lifecycle"}:
        if kind == "context_metadata":
            fields = payload.get("fields", {})
            return {"fields": _redact_value(fields, 200, counters)}
        if kind == "lifecycle":
            return _redact_value(
                {key: payload[key] for key in ("type", "status") if key in payload},
                300,
                counters,
            )
        return _safe_turn_event(payload, counters)
    if kind == "diagnostic":
        allowed = {
            key: payload[key]
            for key in (
                "kind",
                "line",
                "status",
                "record_type",
                "call_id",
                "raw_call_id",
                "source_ref",
            )
            if key in payload
        }
        return _redact_value(allowed, 300, counters)
    return {"type": str(payload.get("type", kind) or kind), "status": "unknown"}


def _canonical_session(session: Session, counters: Dict[str, int]) -> Dict[str, Any]:
    """Serialize the minimum typed session model needed to replay offline."""

    turns: List[Dict[str, Any]] = []
    for turn in session.turns:
        user_input, user_counts = redact_text(turn.user_input)
        assistant_output, assistant_counts = redact_text(turn.assistant_output)
        counters["patterns"] += user_counts["patterns"] + assistant_counts["patterns"]
        counters["truncated_chars"] += user_counts["truncated_chars"] + assistant_counts["truncated_chars"]
        calls: List[Dict[str, Any]] = []
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
                    "raw_call_id": call.raw_call_id or call.call_id,
                    "output": output,
                    "success": call.success,
                    "exit_code": call.exit_code,
                    "status": _safe_status(call),
                    "source_ref": _relative_source_ref(call.source_ref),
                    "result_source_ref": _relative_source_ref(call.result_source_ref),
                    "timestamp": call.timestamp,
                    "result_timestamp": call.result_timestamp,
                    "sequence": call.sequence,
                    "result_sequence": call.result_sequence,
                    "result_metadata": _redact_value(call.result_metadata, 500, counters),
                    "diagnostics": list(call.diagnostics),
                }
            )
        turns.append(
            {
                "index": turn.index,
                "user_input": user_input,
                "assistant_output": assistant_output,
                "tool_calls": calls,
                "events": [
                    _safe_turn_event(event, counters) for event in turn.events
                ],
                "context_meta": _redact_value(turn.context_meta, 200, counters),
                "timestamp": turn.timestamp,
                "raw_tool_output_chars": _observed_output_chars(turn),
                "total_context_chars": turn.total_context_chars,
                "diagnostics": _redact_value(turn.diagnostics, 500, counters),
            }
        )

    safe_metadata: Dict[str, Any] = {}
    for key, value in session.metadata.items():
        if _key_name(key) in _IDENTITY_KEYS:
            safe_metadata[str(key)] = _redact_value(value, 300, counters, key)
    return {
        "id": session.id,
        "source": session.source,
        "model": session.model,
        "cwd": _portable_cwd(session.cwd, counters),
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
        "source_capabilities": _redact_value(session.source_capabilities, 500, counters),
    }


def _fallback_event_log(session: Session) -> List[Dict[str, Any]]:
    """Build an ordered projection for hand-constructed Session objects."""

    result: List[Dict[str, Any]] = []
    sequence = 0
    for turn in session.turns:
        if turn.user_input:
            sequence += 1
            result.append({"sequence": sequence, "kind": "user_message", "turn_index": turn.index, "timestamp": turn.timestamp, "source_ref": session.source_ref, "payload": {"text": turn.user_input}})
        for call in turn.tool_calls:
            sequence += 1
            result.append({"sequence": sequence, "kind": "tool_call", "turn_index": turn.index, "timestamp": call.timestamp or turn.timestamp, "source_ref": call.source_ref or session.source_ref, "payload": {"call_id": call.raw_call_id or call.call_id, "name": call.name, "arguments": call.arguments, "status": _safe_status(call)}})
            if call.output or call.success is not None or call.exit_code is not None:
                sequence += 1
                result.append({"sequence": sequence, "kind": "tool_result", "turn_index": turn.index, "timestamp": call.result_timestamp or turn.timestamp, "source_ref": call.result_source_ref or call.source_ref or session.source_ref, "payload": {"call_id": call.raw_call_id or call.call_id, "output": call.output, "success": call.success, "exit_code": call.exit_code, "result_metadata": call.result_metadata}})
        if turn.assistant_output:
            sequence += 1
            result.append({"sequence": sequence, "kind": "assistant_message", "turn_index": turn.index, "timestamp": turn.timestamp, "source_ref": session.source_ref, "payload": {"text": turn.assistant_output}})
        for event in turn.events:
            sequence += 1
            result.append({"sequence": sequence, "kind": "session_event", "turn_index": turn.index, "timestamp": turn.timestamp, "source_ref": session.source_ref, "payload": event})
    return result


def _build_events(session: Session, limits: BundleLimits, counters: Dict[str, int]) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    raw_events = sorted(session.event_log or _fallback_event_log(session), key=lambda item: int(item.get("sequence", 0) or 0))
    events: List[Dict[str, Any]] = []
    for raw in raw_events:
        if len(events) >= limits.max_events:
            break
        sequence = int(raw.get("sequence", len(events) + 1) or len(events) + 1)
        kind = str(raw.get("kind", "unknown_event") or "unknown_event")
        payload = raw.get("payload", {})
        source_ref = _relative_source_ref(str(raw.get("source_ref", "") or session.source_ref))
        events.append(
            {
                "event_id": _stable_id(session.source, session.id, _relative_source_ref(session.source_ref), sequence, kind, payload.get("call_id", "") if isinstance(payload, Mapping) else ""),
                "sequence": sequence,
                "kind": kind,
                "turn_index": raw.get("turn_index"),
                "timestamp": str(raw.get("timestamp", "") or "") or None,
                "source_ref": source_ref,
                "payload": _safe_event_payload(kind, payload, limits, counters),
            }
        )
    coverage = {
        "status": "complete" if len(events) == len(raw_events) else "truncated",
        "input_turns": len(session.turns),
        "input_events": len(raw_events),
        "exported_events": len(events),
        "max_events": limits.max_events,
        "excluded_events": max(0, len(raw_events) - len(events)),
        "observed_cutoff": session.timestamp_end or None,
    }
    return events, coverage


def _build_facts(session: Session, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    calls = [call for turn in session.turns for call in turn.tool_calls]
    known = [call for call in calls if _safe_status(call) != "unknown"]
    return {
        "session_id": session.id,
        "source": session.source,
        "source_identity": _stable_id(session.source, session.id, _relative_source_ref(session.source_ref)),
        "task_id": session.metadata.get("task_id", session.metadata.get("taskId")),
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
            "task_id": bool(session.metadata.get("task_id") or session.metadata.get("taskId")),
        },
        "raw_tool_output_chars": sum(_observed_output_chars(turn) for turn in session.turns),
    }


def _event_is_verification(event: Mapping[str, Any]) -> bool:
    kind = str(event.get("kind", ""))
    payload = event.get("payload", {})
    if kind == "session_event" and isinstance(payload, Mapping):
        event_type = str(payload.get("type", "")).lower()
        if event_type in {"test_verification", "verification", "validation", "tests_passed"}:
            return True
    if kind == "tool_call" and isinstance(payload, Mapping):
        name = str(payload.get("name", "")).lower()
        args = payload.get("arguments", {})
        command = ""
        if isinstance(args, Mapping):
            command = str(args.get("command", args.get("cmd", "")) or "")
        text = command.strip().lower()
        if name in {"pytest", "unittest", "test", "verify", "check"}:
            return True
        tokens = re.findall(r"[a-zA-Z0-9_.-]+", text)
        if not tokens:
            return False
        if tokens[0] in {"pytest", "py.test", "unittest", "ctest"}:
            return True
        if len(tokens) >= 2 and tokens[0] in {"python", "python3", "make", "cargo", "go", "npm", "pnpm", "yarn"}:
            return (tokens[1] == "test") or (tokens[1] == "-m" and len(tokens) > 2 and tokens[2] in {"pytest", "unittest"})
    return False


def _build_evidence_and_cases(session: Session, events: List[Dict[str, Any]], limits: BundleLimits, counters: Dict[str, int]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    evidence: List[Dict[str, Any]] = []
    cases: List[Dict[str, Any]] = []
    for turn in session.turns:
        if len(cases) >= limits.max_cases:
            break
        turn_events = [event for event in events if event.get("turn_index") == turn.index]
        turn_events.sort(key=lambda event: int(event.get("sequence", 0) or 0))
        claim_events = [event for event in turn_events if event.get("kind") == "assistant_message"]
        claim = claim_events[0] if claim_events else None
        cutoff_sequence = int(claim.get("sequence")) if claim is not None else (int(turn_events[-1].get("sequence")) if turn_events else None)
        cutoff_events = [
            event for event in turn_events
            if cutoff_sequence is None or int(event.get("sequence", 0) or 0) <= cutoff_sequence
        ]
        refs = [str(event["event_id"]) for event in cutoff_events if event.get("event_id")]
        text_parts = [turn.user_input, turn.assistant_output]
        text_parts.extend(call.output for call in turn.tool_calls if call.output)
        safe_text, local = redact_text("\n".join(part for part in text_parts if part), limits.max_evidence_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        ref_id = f"evidence-{_stable_id(session.source, session.id, session.source_ref, turn.index)}"
        cutoff_timestamp = ""
        if claim is not None:
            cutoff_timestamp = str(claim.get("timestamp") or "")
            if not cutoff_timestamp:
                cutoff_timestamp = session.timestamp_end or ""
        elif turn_events:
            cutoff_timestamp = str(turn_events[-1].get("timestamp") or "")
        if not cutoff_timestamp:
            cutoff_timestamp = session.timestamp_end or ""
        evidence.append(
            {
                "ref_id": ref_id,
                "kind": "turn_observation",
                "event_refs": refs,
                "text": safe_text,
                "observation_cutoff": cutoff_timestamp or None,
                "observation_cutoff_sequence": cutoff_sequence,
                "redaction": {"bounded": True, "patterns": local["patterns"]},
            }
        )
        has_verification = any(_event_is_verification(event) for event in cutoff_events)
        cases.append(
            {
                "case_id": f"case-{_stable_id(session.source, session.id, session.source_ref, turn.index)}",
                "kind": "offline_observation_candidate",
                "turn_index": turn.index,
                "evidence_refs": [ref_id],
                "event_refs": refs,
                "relations": {
                    "requirement_to_action": "candidate" if any(event.get("kind") == "user_message" for event in cutoff_events) and any(event.get("kind") == "tool_call" for event in cutoff_events) else "insufficient",
                    "failure_to_disposition_to_result": "candidate" if any(event.get("kind") == "tool_result" and isinstance(event.get("payload"), Mapping) and event["payload"].get("exit_code") not in (None, 0) for event in cutoff_events) else "not_observed",
                    "claim_to_verification": "candidate" if claim is not None and has_verification else "insufficient",
                },
                "observation_cutoff": cutoff_timestamp or None,
                "observation_cutoff_sequence": cutoff_sequence,
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
        if len(session.turns) > limits.max_turns:
            raise BundleError(f"session turn limit exceeded: {limits.max_turns}")
        tool_count = sum(len(turn.tool_calls) for turn in session.turns)
        if tool_count > limits.max_tool_calls:
            raise BundleError(f"session tool-call limit exceeded: {limits.max_tool_calls}")
        counters = {"patterns": 0, "truncated_chars": 0}
        events, coverage = _build_events(session, limits, counters)
        evidence, cases = _build_evidence_and_cases(session, events, limits, counters)
        canonical = _canonical_session(session, counters)
        source_ref = _relative_source_ref(session.source_ref)
        artifact_id = _stable_id(session.source, session.id, source_ref)
        bundle = cls(
            manifest={
                "schema": BUNDLE_SCHEMA,
                "version": BUNDLE_VERSION,
                "schema_version": BUNDLE_VERSION,
                "bundle_version": BUNDLE_VERSION,
                "session_id": session.id,
                "source": session.source,
                "source_ref": source_ref,
                "artifact_id": artifact_id,
                "parser_version": session.parser_version,
                "portable": True,
                "redaction": {
                    "patterns": counters["patterns"],
                    "absolute_paths": counters.get("absolute_paths", 0),
                    "truncated_chars": counters["truncated_chars"],
                    "secret_detection_disclaimer": "Redaction is bounded heuristic detection, not proof that no secret remains.",
                },
            },
            events=events,
            facts=_build_facts(session, events),
            source_capabilities=dict(session.source_capabilities),
            source_refs=sorted({str(event.get("source_ref")) for event in events if event.get("source_ref")}),
            evidence_refs=evidence,
            cases=cases,
            coverage=coverage,
            session=canonical,
            diagnostics=list(session.diagnostics),
        )
        encoded = bundle.to_json()
        if len(encoded.encode("utf-8")) > limits.max_bytes:
            raise BundleError(f"bundle exceeds max_bytes={limits.max_bytes}; reduce input or raise the explicit limit")
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
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any], limits: BundleLimits | None = None) -> "SessionBundle":
        if not isinstance(payload, Mapping):
            raise BundleError("bundle root must be an object")
        limits = limits or BundleLimits()
        _validate_json_tree(payload)
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
        encoded_size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        if encoded_size > limits.max_bytes:
            raise BundleError("bundle byte limit exceeded")
        for field_name in ("manifest", "facts", "source_capabilities", "coverage"):
            if not isinstance(payload.get(field_name, {}), Mapping):
                raise BundleError(f"bundle {field_name} must be an object")
        if not isinstance(payload.get("diagnostics", []), list):
            raise BundleError("bundle diagnostics must be a list")
        manifest = payload.get("manifest", {})
        if manifest.get("source_ref") is not None:
            _validate_relative_ref(manifest["source_ref"])
        events = payload.get("events", [])
        if not isinstance(events, list):
            raise BundleError("bundle events must be a list")
        if len(events) > limits.max_events:
            raise BundleError("bundle event limit exceeded")
        cases = payload.get("cases", [])
        evidence = payload.get("evidence_refs", [])
        if not isinstance(cases, list) or len(cases) > limits.max_cases:
            raise BundleError("bundle case limit exceeded")
        if not isinstance(evidence, list) or len(evidence) > limits.max_cases:
            raise BundleError("bundle evidence limit exceeded")
        event_ids: set[str] = set()
        sequences: set[int] = set()
        for event in events:
            if not isinstance(event, Mapping):
                raise BundleError("bundle event must be an object")
            event_id = event.get("event_id")
            sequence = event.get("sequence")
            if not isinstance(event_id, str) or not event_id or event_id in event_ids:
                raise BundleError("bundle event IDs must be unique non-empty strings")
            if not isinstance(sequence, int) or isinstance(sequence, bool) or sequence <= 0 or sequence in sequences:
                raise BundleError("bundle event sequences must be unique positive integers")
            if not isinstance(event.get("kind"), str) or not isinstance(event.get("payload", {}), Mapping):
                raise BundleError("bundle event kind/payload schema is invalid")
            event_ids.add(event_id)
            sequences.add(sequence)
            if event.get("turn_index") is not None and (
                not isinstance(event.get("turn_index"), int)
                or isinstance(event.get("turn_index"), bool)
            ):
                raise BundleError("bundle event turn_index must be an integer or null")
            if event.get("timestamp") is not None and not isinstance(event.get("timestamp"), str):
                raise BundleError("bundle event timestamp must be a string or null")
            _validate_relative_ref(event.get("source_ref", ""))
        evidence_ids: set[str] = set()
        for item in evidence:
            if not isinstance(item, Mapping) or not isinstance(item.get("ref_id"), str) or not item.get("ref_id") or item["ref_id"] in evidence_ids:
                raise BundleError("bundle evidence refs must have unique ref_id values")
            if not isinstance(item.get("kind", ""), str) or not isinstance(item.get("text", ""), str):
                raise BundleError("bundle evidence kind/text schema is invalid")
            _validate_refs(item.get("event_refs", []), event_ids, "evidence event_refs")
            evidence_ids.add(item["ref_id"])
        case_ids: set[str] = set()
        for item in cases:
            if not isinstance(item, Mapping) or not isinstance(item.get("case_id"), str) or not item.get("case_id") or item["case_id"] in case_ids:
                raise BundleError("bundle case IDs must be unique non-empty strings")
            if not isinstance(item.get("kind", ""), str) or not isinstance(item.get("relations", {}), Mapping):
                raise BundleError("bundle case kind/relations schema is invalid")
            _validate_refs(item.get("event_refs", []), event_ids, "case event_refs")
            _validate_refs(item.get("evidence_refs", []), evidence_ids, "case evidence_refs")
            cutoff = item.get("observation_cutoff_sequence")
            if cutoff is not None and (not isinstance(cutoff, int) or isinstance(cutoff, bool) or cutoff <= 0):
                raise BundleError("case observation cutoff sequence is invalid")
            case_ids.add(item["case_id"])
        source_refs = payload.get("source_refs", [])
        if not isinstance(source_refs, list) or any(not isinstance(ref, str) for ref in source_refs):
            raise BundleError("bundle source_refs must be a list of strings")
        for ref in source_refs:
            _validate_relative_ref(ref)
        session_payload = payload.get("session", {})
        if not isinstance(session_payload, Mapping):
            raise BundleError("bundle session must be an object")
        turns = session_payload.get("turns", [])
        if not isinstance(turns, list) or len(turns) > limits.max_turns:
            raise BundleError("bundle turn limit exceeded")
        total_calls = 0
        for turn in turns:
            if not isinstance(turn, Mapping) or not isinstance(turn.get("tool_calls", []), list):
                raise BundleError("bundle turn/tool_calls schema is invalid")
            if not isinstance(turn.get("index"), int) or isinstance(turn.get("index"), bool):
                raise BundleError("bundle turn index must be an integer")
            for text_field in ("user_input", "assistant_output", "timestamp"):
                if not isinstance(turn.get(text_field, ""), str):
                    raise BundleError(f"bundle turn {text_field} must be a string")
            if not isinstance(turn.get("events", []), list) or not isinstance(turn.get("context_meta", {}), Mapping):
                raise BundleError("bundle turn event/context schema is invalid")
            if not isinstance(turn.get("diagnostics", []), list):
                raise BundleError("bundle turn diagnostics must be a list")
            for raw_call in turn.get("tool_calls", []):
                if not isinstance(raw_call, Mapping):
                    raise BundleError("bundle tool call must be an object")
                if not isinstance(raw_call.get("name", ""), str) or not isinstance(raw_call.get("arguments", {}), Mapping):
                    raise BundleError("bundle tool call name/arguments schema is invalid")
                for call_text in ("call_id", "raw_call_id", "output", "status", "source_ref", "result_source_ref", "timestamp", "result_timestamp"):
                    if not isinstance(raw_call.get(call_text, ""), str):
                        raise BundleError(f"bundle tool call {call_text} must be a string")
                success = raw_call.get("success")
                if success is not None and not isinstance(success, bool):
                    raise BundleError("bundle tool call success must be boolean or null")
                exit_code = raw_call.get("exit_code")
                if exit_code is not None and (
                    not isinstance(exit_code, int) or isinstance(exit_code, bool)
                ):
                    raise BundleError("bundle tool call exit_code must be an integer or null")
                for numeric_field in ("sequence", "result_sequence"):
                    value = raw_call.get(numeric_field, 0)
                    if not isinstance(value, int) or isinstance(value, bool):
                        raise BundleError(f"bundle tool call {numeric_field} must be an integer")
                if not isinstance(raw_call.get("result_metadata", {}), Mapping):
                    raise BundleError("bundle tool call result_metadata must be an object")
                if not isinstance(raw_call.get("diagnostics", []), list):
                    raise BundleError("bundle tool call diagnostics must be a list")
                for ref_field in ("source_ref", "result_source_ref"):
                    if raw_call.get(ref_field):
                        _validate_relative_ref(raw_call[ref_field])
            total_calls += len(turn.get("tool_calls", []))
        if total_calls > limits.max_tool_calls:
            raise BundleError("bundle tool-call limit exceeded")
        return cls(
            manifest=dict(payload.get("manifest", {})) if isinstance(payload.get("manifest", {}), Mapping) else {},
            events=list(events),
            facts=dict(payload.get("facts", {})) if isinstance(payload.get("facts", {}), Mapping) else {},
            source_capabilities=dict(payload.get("source_capabilities", {})) if isinstance(payload.get("source_capabilities", {}), Mapping) else {},
            source_refs=[str(item) for item in source_refs],
            evidence_refs=list(evidence),
            cases=list(cases),
            coverage=dict(payload.get("coverage", {})) if isinstance(payload.get("coverage", {}), Mapping) else {},
            session=dict(session_payload),
            diagnostics=list(payload.get("diagnostics", [])) if isinstance(payload.get("diagnostics", []), list) else [],
        )

    @classmethod
    def from_json(cls, text: str, limits: BundleLimits | None = None) -> "SessionBundle":
        limits = limits or BundleLimits()
        if len(text.encode("utf-8")) > limits.max_bytes:
            raise BundleError("bundle byte limit exceeded")
        try:
            payload = json.loads(text, parse_constant=_reject_non_finite)
        except (json.JSONDecodeError, ValueError) as exc:
            raise BundleError(f"invalid bundle JSON: {exc}") from exc
        return cls.from_dict(payload, limits=limits)

    def to_session(self) -> Session:
        """Reconstruct the normalized Session used by offline metrics."""

        raw = self.session
        if not raw:
            raise BundleError("bundle does not contain a replayable session")
        session = Session(
            id=str(raw.get("id", self.manifest.get("session_id", ""))),
            source=str(raw.get("source", self.manifest.get("source", "unknown"))),
            model=str(raw.get("model", "")),
            cwd=str(raw.get("cwd", "")),
            cli_version=str(raw.get("cli_version", "")),
            metadata=dict(raw.get("metadata", {})) if isinstance(raw.get("metadata", {}), Mapping) else {},
            timestamp_start=str(raw.get("timestamp_start", "")),
            timestamp_end=str(raw.get("timestamp_end", "")),
            context_compacted_count=int(raw.get("context_compacted_count", 0) or 0),
            task_started_count=int(raw.get("task_started_count", 0) or 0),
            task_complete_count=int(raw.get("task_complete_count", 0) or 0),
            turn_aborted_count=int(raw.get("turn_aborted_count", 0) or 0),
            diagnostics=list(raw.get("diagnostics", self.diagnostics)),
            parser_version=str(raw.get("parser_version", "bundle-1")),
            source_ref=str(raw.get("source_ref", self.manifest.get("source_ref", "session.jsonl"))),
            source_capabilities=dict(raw.get("source_capabilities", self.source_capabilities)) if isinstance(raw.get("source_capabilities", self.source_capabilities), Mapping) else dict(self.source_capabilities),
            event_log=list(self.events),
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
                    arguments=dict(raw_call.get("arguments", {})) if isinstance(raw_call.get("arguments", {}), Mapping) else {},
                    call_id=str(raw_call.get("call_id", "")),
                    raw_call_id=str(raw_call.get("raw_call_id", raw_call.get("call_id", ""))),
                    output=str(raw_call.get("output", "")),
                    success=raw_call.get("success") if isinstance(raw_call.get("success"), bool) else None,
                    exit_code=raw_call.get("exit_code") if isinstance(raw_call.get("exit_code"), int) and not isinstance(raw_call.get("exit_code"), bool) else None,
                    status=str(raw_call.get("status", "unknown")),
                    source_ref=str(raw_call.get("source_ref", "")),
                    result_source_ref=str(raw_call.get("result_source_ref", "")),
                    timestamp=str(raw_call.get("timestamp", "")),
                    result_timestamp=str(raw_call.get("result_timestamp", "")),
                    sequence=int(raw_call.get("sequence", 0) or 0),
                    result_sequence=int(raw_call.get("result_sequence", 0) or 0),
                    result_metadata=dict(raw_call.get("result_metadata", {})) if isinstance(raw_call.get("result_metadata", {}), Mapping) else {},
                    diagnostics=[str(item) for item in raw_call.get("diagnostics", [])] if isinstance(raw_call.get("diagnostics", []), list) else [],
                )
                turn.tool_calls.append(call)
            session.turns.append(turn)
        return session


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


def _validate_json_tree(value: Any, path: str = "bundle") -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise BundleError(f"non-finite number at {path}")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise BundleError(f"non-string key at {path}")
            _validate_json_tree(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{path}[{index}]")
        return
    raise BundleError(f"unsupported value at {path}")


def _validate_relative_ref(value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise BundleError("bundle source refs must be non-empty strings")
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or ".." in Path(normalized.split("#", 1)[0]).parts:
        raise BundleError("bundle source ref must be relative")


def _validate_refs(value: Any, allowed: set[str], label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or item not in allowed for item in value):
        raise BundleError(f"{label} contains an unknown reference")


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
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BundleError("bundle is not valid UTF-8") from exc
    return SessionBundle.from_json(text, limits=limits)


load_bundle = import_bundle
session_from_bundle = lambda bundle: bundle.to_session() if isinstance(bundle, SessionBundle) else SessionBundle.from_dict(bundle).to_session()
