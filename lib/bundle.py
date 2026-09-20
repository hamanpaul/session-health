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

from .parser_base import Session, ToolCall, Turn, argument_fingerprint, command_fingerprint


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

    def __post_init__(self) -> None:
        for name, value in (
            ("max_events", self.max_events),
            ("max_bytes", self.max_bytes),
            ("max_evidence_chars", self.max_evidence_chars),
            ("max_cases", self.max_cases),
            ("max_turns", self.max_turns),
            ("max_tool_calls", self.max_tool_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


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
_CAPABILITY_KEYS = {
    "format",
    "supports_nested_response_items",
    "supports_json_string_arguments",
    "supports_call_result_pairing",
    "supports_structured_exit_code",
    "outcome_states",
    "input_limits",
    "lifecycle_pairing",
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


_INPUT_PARTIAL_KINDS = {
    "input_byte_limit_exceeded",
    "record_limit_exceeded",
    "oversize_record",
    "malformed_record",
    "non_object_record",
    "malformed_payload",
    "bundle_input_coverage",
}


def _input_coverage(session: Session) -> Dict[str, Any]:
    """Classify source-read completeness separately from evidence projection.

    Parser diagnostics can describe a complete source with ambiguous semantic
    records (for example duplicate call IDs).  Only diagnostics that mean
    records were not fully readable/usable affect the source-read contract.
    """

    kinds = sorted(
        {
            str(item.get("kind", "unknown"))
            for item in session.diagnostics
            if isinstance(item, Mapping)
        }
    )
    if "parse_failure" in kinds:
        status = "failed"
    elif any(kind in _INPUT_PARTIAL_KINDS for kind in kinds):
        status = "partial"
    else:
        status = "complete"
    return {
        "status": status,
        "complete": status == "complete",
        "diagnostic_kinds": kinds,
    }


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


_SNR_FACT_NAMES = ("total_chars", "noise_chars", "ansi_chars", "progress_chars", "duplicate_chars")


def _snr_snapshot(turn: Turn) -> Dict[str, int]:
    """Return stable typed SNR facts, including on a replayed projection."""

    facts = turn.snr_facts
    if facts and all(
        isinstance(facts.get(name), int)
        and not isinstance(facts.get(name), bool)
        and facts.get(name, 0) >= 0
        for name in _SNR_FACT_NAMES
    ):
        return {name: int(facts[name]) for name in _SNR_FACT_NAMES}
    from .metrics.snr import analyze_snr

    result = analyze_snr(turn)
    return {
        "total_chars": int(result.total_chars),
        "noise_chars": int(result.noise_chars),
        "ansi_chars": int(result.ansi_chars),
        "progress_chars": int(result.progress_chars),
        "duplicate_chars": int(result.duplicate_chars),
    }


def _redact_metric_value(value: Any, max_chars: int, counters: Dict[str, int], key: Any = "") -> Any:
    """Redact bounded metric identity while retaining both command edges."""

    if _key_name(key) in _SENSITIVE_KEYS:
        counters["patterns"] += 1
        return "[REDACTED]"
    if isinstance(value, str):
        # Redact before retaining the suffix so a secret cannot be preserved by
        # the edge-aware truncation.
        redacted, local = redact_text(value, max_chars=max(len(value), max_chars))
        counters["patterns"] += local["patterns"]
        if "absolute_paths" in local:
            counters["absolute_paths"] = counters.get("absolute_paths", 0) + local["absolute_paths"]
        if len(redacted) <= max_chars:
            return redacted
        marker = "…[TRUNCATED]…"
        available = max(1, max_chars - len(marker))
        head = available // 2
        tail = available - head
        counters["truncated_chars"] += len(redacted) - max_chars
        return redacted[:head] + marker + redacted[-tail:]
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact_metric_value(item, max_chars, counters, item_key)
            for item_key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_metric_value(item, max_chars, counters) for item in value]
    return value


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


def _safe_diagnostic(value: Any, counters: Dict[str, int]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"kind": "unknown", "status": "unknown"}
    allowed = {
        key: value[key]
        for key in ("kind", "line", "status", "record_type", "call_id", "raw_call_id", "source_ref")
        if key in value
    }
    return _redact_value(allowed, 500, counters)


def _safe_capabilities(value: Any, counters: Dict[str, int]) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    allowed = {key: value[key] for key in _CAPABILITY_KEYS if key in value}
    return _redact_value(allowed, 500, counters)


def _canonical_session(
    session: Session,
    counters: Dict[str, int],
    events: List[Dict[str, Any]] | None = None,
) -> Dict[str, Any]:
    """Serialize the minimum typed session model needed to replay offline."""

    # Keep the complete numeric noise facts even when the textual output below
    # is bounded.  Import locally to avoid making the parser/bundle layer
    # depend on process-v2 at module import time.
    projection: Dict[int, Dict[str, Any]] | None = None
    if events is not None:
        projection = {}
        for event in events:
            turn_index = event.get("turn_index")
            if not isinstance(turn_index, int):
                continue
            item = projection.setdefault(
                turn_index,
                {"kinds": set(), "calls": [], "results": [], "events": []},
            )
            kind = str(event.get("kind", ""))
            item["kinds"].add(kind)
            if kind == "tool_call":
                item["calls"].append(event)
            elif kind == "tool_result":
                item["results"].append(event)
            elif kind in {"session_event", "context_metadata", "lifecycle"}:
                item["events"].append(event)

    def matching_event(
        candidates: List[Dict[str, Any]],
        call: ToolCall,
        *,
        result: bool = False,
        used: set[str] | None = None,
    ) -> Dict[str, Any] | None:
        call_ref = _relative_source_ref(
            call.result_source_ref if result else call.source_ref
        )
        sequence = call.result_sequence if result else call.sequence
        raw_id = call.raw_call_id or call.call_id
        if result and not sequence and not call_ref:
            available = [
                candidate
                for candidate in candidates
                if used is None or str(candidate.get("event_id", "")) not in used
            ]
            if len(available) != 1:
                return None
        for event in candidates:
            event_id = str(event.get("event_id", ""))
            if used is not None and event_id in used:
                continue
            payload = event.get("payload", {})
            event_call_id = payload.get("call_id") if isinstance(payload, Mapping) else None
            if sequence and event.get("sequence") == sequence:
                return event
            if call_ref and _relative_source_ref(str(event.get("source_ref", ""))) == call_ref:
                return event
            if event_call_id is not None and str(event_call_id) == str(raw_id):
                return event
        return None

    turns: List[Dict[str, Any]] = []
    for turn in session.turns:
        turn_projection = projection.get(turn.index) if projection is not None else None
        if projection is not None and turn_projection is None:
            continue
        snr = _snr_snapshot(turn)
        observed_total = max(int(turn.raw_tool_output_chars or 0), snr["total_chars"])
        kinds = turn_projection["kinds"] if turn_projection is not None else {
            "user_message",
            "assistant_message",
            "tool_call",
            "tool_result",
            "session_event",
            "context_metadata",
        }
        user_input, user_counts = redact_text(
            turn.user_input if "user_message" in kinds else ""
        )
        assistant_output, assistant_counts = redact_text(
            turn.assistant_output if "assistant_message" in kinds else ""
        )
        counters["patterns"] += user_counts["patterns"] + assistant_counts["patterns"]
        counters["truncated_chars"] += user_counts["truncated_chars"] + assistant_counts["truncated_chars"]
        calls: List[Dict[str, Any]] = []
        used_call_events: set[str] = set()
        used_result_events: set[str] = set()
        for call in turn.tool_calls:
            call_event = matching_event(
                turn_projection["calls"] if turn_projection is not None else [],
                call,
                used=used_call_events,
            ) if turn_projection is not None else {"event_id": "full"}
            if call_event is None:
                continue
            if turn_projection is not None:
                used_call_events.add(str(call_event.get("event_id", "")))
            result_event = matching_event(
                turn_projection["results"] if turn_projection is not None else [],
                call,
                result=True,
                used=used_result_events,
            ) if turn_projection is not None else {"event_id": "full"}
            has_result = result_event is not None
            if has_result and turn_projection is not None:
                used_result_events.add(str(result_event.get("event_id", "")))
            arguments = _redact_value(call.arguments, 1_000, counters)
            output, output_counts = redact_text(call.output if has_result else "")
            counters["patterns"] += output_counts["patterns"]
            counters["truncated_chars"] += output_counts["truncated_chars"]
            calls.append(
                {
                    "name": call.name,
                    "arguments": arguments,
                    "argument_fingerprint": call.argument_fingerprint or argument_fingerprint(call.arguments),
                    "command_fingerprint": call.command_fingerprint or command_fingerprint(call.arguments),
                    "call_id": call.call_id,
                    "raw_call_id": call.raw_call_id or call.call_id,
                    "output": output,
                    "success": call.success if has_result else None,
                    "exit_code": call.exit_code if has_result else None,
                    "status": _safe_status(call) if has_result else "unknown",
                    "source_ref": _relative_source_ref(call.source_ref),
                    "result_source_ref": _relative_source_ref(call.result_source_ref) if has_result else "",
                    "timestamp": call.timestamp,
                    "result_timestamp": call.result_timestamp if has_result else "",
                    "sequence": call.sequence,
                    "result_sequence": call.result_sequence if has_result else 0,
                    "result_metadata": _redact_value(call.result_metadata, 500, counters) if has_result else {},
                    "diagnostics": list(call.diagnostics) if has_result else ["result_not_in_projection"],
                }
            )
        retained_events = []
        if turn_projection is None:
            retained_events = [_safe_turn_event(event, counters) for event in turn.events]
        else:
            for event in turn_projection["events"]:
                payload = event.get("payload", {})
                if event.get("kind") == "session_event" and isinstance(payload, Mapping):
                    retained_events.append(_safe_turn_event(payload, counters))
        turns.append(
            {
                "index": turn.index,
                "user_input": user_input,
                "assistant_output": assistant_output,
                "tool_calls": calls,
                "events": retained_events,
                "context_meta": _redact_value(
                    turn.context_meta if "context_metadata" in kinds else {},
                    200,
                    counters,
                ),
                "timestamp": turn.timestamp,
                "raw_tool_output_chars": _observed_output_chars(turn),
                "total_context_chars": turn.total_context_chars,
                "snr_facts": {
                    "total_chars": observed_total,
                    "noise_chars": snr["noise_chars"],
                    "ansi_chars": snr["ansi_chars"],
                    "progress_chars": snr["progress_chars"],
                    "duplicate_chars": snr["duplicate_chars"],
                },
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
        "diagnostics": [_safe_diagnostic(item, counters) for item in session.diagnostics],
        "parser_version": session.parser_version,
        "source_ref": _relative_source_ref(session.source_ref),
        "source_capabilities": _safe_capabilities(session.source_capabilities, counters),
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


def _build_events(
    session: Session,
    limits: BundleLimits,
    counters: Dict[str, int],
    event_limit: int | None = None,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    raw_events = sorted(session.event_log or _fallback_event_log(session), key=lambda item: int(item.get("sequence", 0) or 0))
    events: List[Dict[str, Any]] = []
    export_limit = event_limit if event_limit is not None else limits.max_events
    projected_order = not session.event_log or all(
        event.get("ordering") == "projected" for event in session.event_log
    )
    for raw in raw_events:
        if len(events) >= export_limit:
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
                "timestamp": None if projected_order else str(raw.get("timestamp", "") or "") or None,
                "source_ref": source_ref,
                "ordering": "projected" if projected_order else "observed",
                "payload": _safe_event_payload(kind, payload, limits, counters),
            }
        )
    coverage = {
        "status": "complete" if len(events) == len(raw_events) else "truncated",
        "input_turns": len(session.turns),
        "input_events": len(raw_events),
        "exported_events": len(events),
        "max_events": limits.max_events,
        "export_budget": export_limit,
        "excluded_events": max(0, len(raw_events) - len(events)),
        "observed_cutoff": session.timestamp_end or None if not projected_order else None,
        "ordering": "projected" if projected_order else "observed",
        "temporal_support": not projected_order,
    }
    return events, coverage


def _build_facts(
    session: Session,
    events: List[Dict[str, Any]],
    counters: Dict[str, int],
    input_coverage: Mapping[str, Any] | None = None,
) -> Dict[str, Any]:
    calls = [call for turn in session.turns for call in turn.tool_calls]
    known = [call for call in calls if _safe_status(call) != "unknown"]
    turn_facts: List[Dict[str, Any]] = []
    for turn in session.turns:
        snr = _snr_snapshot(turn)
        turn_facts.append(
            {
                "index": turn.index,
                "timestamp": turn.timestamp,
                "tool_call_count": len(turn.tool_calls),
                "raw_tool_output_chars": _observed_output_chars(turn),
                "snr_facts": {
                    "total_chars": max(_observed_output_chars(turn), snr["total_chars"]),
                    "noise_chars": snr["noise_chars"],
                    "ansi_chars": snr["ansi_chars"],
                    "progress_chars": snr["progress_chars"],
                    "duplicate_chars": snr["duplicate_chars"],
                },
                "context_fields": sorted(
                    str(key) for key, value in turn.context_meta.items() if value is True
                ),
                "context_values": {
                    str(key): _redact_value(value, 100, counters, key)
                    for key, value in turn.context_meta.items()
                    if key in {"task_ref", "session_ref", "continuity_event"}
                },
                "context_presence": {
                    str(key): bool(value)
                    for key, value in turn.context_meta.items()
                    if re.sub(r"[^a-z0-9]", "", str(key).lower()) in {
                        "cwdpresent",
                        "exitcodepresent",
                        "permissionpresent",
                        "gitpresent",
                    }
                },
                "event_types": sorted(
                    str(event.get("type", ""))
                    for event in turn.events
                    if isinstance(event, Mapping) and event.get("type")
                ),
            }
        )
    call_facts: List[Dict[str, Any]] = []
    for turn in session.turns:
        for call in turn.tool_calls:
            call_facts.append(
                {
                    "turn_index": turn.index,
                    "name": call.name,
                    "arguments": _redact_metric_value(call.arguments, 1_000, counters),
                    "argument_fingerprint": call.argument_fingerprint or argument_fingerprint(call.arguments),
                    "command_fingerprint": call.command_fingerprint or command_fingerprint(call.arguments),
                    "call_id": call.call_id,
                    "raw_call_id": call.raw_call_id or call.call_id,
                    "status": _safe_status(call),
                    "success": call.success,
                    "exit_code": call.exit_code,
                    "output_chars": len(call.output or ""),
                    "result_metadata": _redact_value(call.result_metadata, 300, counters),
                }
            )
    lifecycle_facts: List[Dict[str, Any]] = []
    if session.lifecycle_facts:
        lifecycle_source = [
            {"kind": "session_event", "payload": event}
            for event in session.lifecycle_facts
            if isinstance(event, Mapping)
        ]
    else:
        lifecycle_source = session.event_log or [
            {"kind": "session_event", "payload": event}
            for turn in session.turns
            for event in turn.events
            if isinstance(event, Mapping)
        ]
    for event in lifecycle_source:
        if not isinstance(event, Mapping) or event.get("kind") != "session_event":
            continue
        payload = event.get("payload", {})
        if not isinstance(payload, Mapping) or not payload.get("type"):
            continue
        item: Dict[str, Any] = {"type": str(payload.get("type"))}
        for key in (
            "task_ref",
            "task_id",
            "taskId",
            "session_ref",
            "session_id",
            "sessionId",
            "status",
            "reason",
        ):
            if key in payload:
                item[key] = _redact_value(payload[key], 100, counters, key)
        lifecycle_facts.append(item)
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
        "metric_facts": {
            "turns": turn_facts,
            "calls": call_facts,
            "lifecycle": lifecycle_facts,
            # ``complete`` means the typed facts are complete for the
            # observed prefix.  It must remain true for bounded raw input so
            # replay can retain valid facts without pretending the source was
            # complete.
            "complete": True,
            "scope": "observed_input",
            "input_status": str((input_coverage or {}).get("status", "complete")),
            "input_complete": bool((input_coverage or {}).get("complete", True)),
        },
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
    observed_order = bool(session.event_log) and not all(
        event.get("ordering") == "projected" for event in session.event_log
    )
    source_ref = _relative_source_ref(session.source_ref)
    for turn in session.turns:
        turn_events = [event for event in events if event.get("turn_index") == turn.index]
        turn_events.sort(key=lambda event: int(event.get("sequence", 0) or 0))
        if not turn_events:
            continue
        if len(cases) >= limits.max_cases:
            break
        claim_events = [event for event in turn_events if event.get("kind") == "assistant_message"]
        claim = claim_events[0] if claim_events and observed_order else None
        cutoff_sequence = (
            int(claim.get("sequence"))
            if claim is not None
            else (int(turn_events[-1].get("sequence")) if turn_events and observed_order else None)
        )
        cutoff_events = [
            event for event in turn_events
            if cutoff_sequence is None or int(event.get("sequence", 0) or 0) <= cutoff_sequence
        ]
        refs = [str(event["event_id"]) for event in cutoff_events if event.get("event_id")]
        text_parts: List[str] = []
        for event in cutoff_events:
            payload = event.get("payload", {})
            if not isinstance(payload, Mapping):
                continue
            if event.get("kind") in {"user_message", "assistant_message"}:
                text_parts.append(str(payload.get("text", "") or ""))
            elif event.get("kind") == "tool_result":
                text_parts.append(str(payload.get("output", "") or ""))
        safe_text, local = redact_text("\n".join(part for part in text_parts if part), limits.max_evidence_chars)
        counters["patterns"] += local["patterns"]
        counters["truncated_chars"] += local["truncated_chars"]
        ref_id = f"evidence-{_stable_id(session.source, session.id, source_ref, turn.index)}"
        cutoff_timestamp = ""
        if claim is not None and observed_order:
            cutoff_timestamp = str(claim.get("timestamp") or "")
        elif turn_events and observed_order:
            cutoff_timestamp = str(turn_events[-1].get("timestamp") or "")
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
        has_verification = observed_order and any(_event_is_verification(event) for event in cutoff_events)
        cases.append(
            {
                "case_id": f"case-{_stable_id(session.source, session.id, source_ref, turn.index)}",
                "kind": "offline_observation_candidate",
                "turn_index": turn.index,
                "evidence_refs": [ref_id],
                "event_refs": refs,
                "relations": {
                    "requirement_to_action": "candidate" if observed_order and any(event.get("kind") == "user_message" for event in cutoff_events) and any(event.get("kind") == "tool_call" for event in cutoff_events) else "unknown" if not observed_order else "insufficient",
                    "failure_to_disposition_to_result": "candidate" if observed_order and any(event.get("kind") == "tool_result" and isinstance(event.get("payload"), Mapping) and event["payload"].get("exit_code") not in (None, 0) for event in cutoff_events) else "unknown" if not observed_order else "not_observed",
                    "claim_to_verification": "candidate" if claim is not None and has_verification else "unknown" if not observed_order else "insufficient",
                },
                "observation_cutoff": cutoff_timestamp or None,
                "observation_cutoff_sequence": cutoff_sequence,
                "ordering": "observed" if observed_order else "projected",
                "temporal_support": observed_order,
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
        source_ref = _relative_source_ref(session.source_ref)
        artifact_id = _stable_id(session.source, session.id, source_ref)

        def assemble(event_budget: int) -> "SessionBundle":
            counters = {"patterns": 0, "truncated_chars": 0}
            events, coverage = _build_events(session, limits, counters, event_limit=event_budget)
            evidence, cases = _build_evidence_and_cases(session, events, limits, counters)
            canonical = _canonical_session(session, counters, events=events)
            input_coverage = _input_coverage(session)
            facts = _build_facts(session, events, counters, input_coverage)
            source_capabilities = _safe_capabilities(session.source_capabilities, counters)
            coverage["canonical_turns"] = len(canonical.get("turns", []))
            coverage["canonical_calls"] = sum(
                len(turn.get("tool_calls", []))
                for turn in canonical.get("turns", [])
            )
            evidence_status = str(coverage.get("status", "complete"))
            coverage.update(
                {
                    "status": (
                        str(input_coverage["status"])
                        if input_coverage["status"] != "complete"
                        else evidence_status
                    ),
                    "input_status": input_coverage["status"],
                    "input_complete": input_coverage["complete"],
                    "input_diagnostic_kinds": input_coverage["diagnostic_kinds"],
                    "evidence_status": evidence_status,
                    "facts_status": "complete_observed_input",
                    "facts_complete": True,
                }
            )
            return cls(
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
                facts=facts,
                source_capabilities=source_capabilities,
                source_refs=sorted({str(event.get("source_ref")) for event in events if event.get("source_ref")}),
                evidence_refs=evidence,
                cases=cases,
                coverage=coverage,
                session=canonical,
                diagnostics=[_safe_diagnostic(item, counters) for item in session.diagnostics],
            )

        event_budget = limits.max_events
        while True:
            bundle = assemble(event_budget)
            encoded_size = len(bundle.to_json().encode("utf-8"))
            if encoded_size <= limits.max_bytes:
                return bundle
            if event_budget <= 1:
                raise BundleError(
                    f"bundle exceeds max_bytes={limits.max_bytes}; reduce input or raise the explicit limit"
                )
            # Evidence, cases, and the canonical projection all use the same
            # event budget.  Reduce that projection until the byte contract is
            # met, while facts retains bounded typed sufficient statistics for
            # the complete parsed input.
            event_budget = max(1, event_budget // 2)

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
        metric_facts = payload.get("facts", {}).get("metric_facts")
        if metric_facts is not None:
            if not isinstance(metric_facts, Mapping):
                raise BundleError("bundle metric_facts must be an object")
            fact_turns = metric_facts.get("turns", [])
            fact_calls = metric_facts.get("calls", [])
            if not isinstance(fact_turns, list) or len(fact_turns) > limits.max_turns:
                raise BundleError("bundle metric fact turn limit exceeded")
            if not isinstance(fact_calls, list) or len(fact_calls) > limits.max_tool_calls:
                raise BundleError("bundle metric fact tool-call limit exceeded")
            for fact_call in fact_calls:
                if isinstance(fact_call, Mapping) and "argument_fingerprint" in fact_call:
                    _validate_argument_fingerprint(fact_call["argument_fingerprint"])
                if isinstance(fact_call, Mapping) and "command_fingerprint" in fact_call:
                    _validate_argument_fingerprint(fact_call["command_fingerprint"])
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
        if session_payload.get("source_ref") is not None:
            _validate_relative_ref(session_payload["source_ref"])
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
            snr_facts = turn.get("snr_facts", {})
            if not isinstance(snr_facts, Mapping):
                raise BundleError("bundle turn snr_facts must be an object")
            for fact_name in ("total_chars", "noise_chars", "ansi_chars", "progress_chars", "duplicate_chars"):
                fact_value = snr_facts.get(fact_name, 0)
                if not isinstance(fact_value, int) or isinstance(fact_value, bool) or fact_value < 0:
                    raise BundleError(f"bundle turn snr_facts.{fact_name} must be a non-negative integer")
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
                if "argument_fingerprint" in raw_call:
                    _validate_argument_fingerprint(raw_call["argument_fingerprint"])
                if "command_fingerprint" in raw_call:
                    _validate_argument_fingerprint(raw_call["command_fingerprint"])
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
        input_status = str(self.coverage.get("input_status", "complete"))
        if input_status != "complete" and not any(
            isinstance(item, Mapping) and item.get("kind") == "bundle_input_coverage"
            for item in session.diagnostics
        ):
            session.diagnostics.append(
                {
                    "kind": "bundle_input_coverage",
                    "status": input_status,
                }
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
                snr_facts={
                    str(key): int(value)
                    for key, value in raw_turn.get("snr_facts", {}).items()
                    if isinstance(value, int) and not isinstance(value, bool)
                },
            )
            for raw_call in raw_turn.get("tool_calls", []):
                call = ToolCall(
                    name=str(raw_call.get("name", "unknown")),
                    arguments=dict(raw_call.get("arguments", {})) if isinstance(raw_call.get("arguments", {}), Mapping) else {},
                    argument_fingerprint=str(raw_call.get("argument_fingerprint", "")),
                    command_fingerprint=str(raw_call.get("command_fingerprint", "")),
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

        # A bounded canonical projection may omit later turns/calls from the
        # evidence surface.  Rehydrate only the redacted typed metric facts so
        # offline replay can retain numeric coverage without restoring raw
        # payloads or bypassing the portable evidence cap.
        metric_facts = self.facts.get("metric_facts", {})
        if isinstance(metric_facts, Mapping) and metric_facts.get("complete") is True:
            raw_lifecycle_facts = metric_facts.get("lifecycle", [])
            if isinstance(raw_lifecycle_facts, list):
                session.lifecycle_facts = [
                    dict(item)
                    for item in raw_lifecycle_facts
                    if isinstance(item, Mapping)
                ]
            turns_by_index = {turn.index: turn for turn in session.turns}
            raw_fact_turns = metric_facts.get("turns", [])
            if isinstance(raw_fact_turns, list):
                for raw_fact in raw_fact_turns:
                    if not isinstance(raw_fact, Mapping):
                        continue
                    try:
                        index = int(raw_fact.get("index", 0))
                    except (TypeError, ValueError):
                        continue
                    if index <= 0:
                        continue
                    turn = turns_by_index.get(index)
                    if turn is None:
                        turn = Turn(index=index, timestamp=str(raw_fact.get("timestamp", "")))
                        session.turns.append(turn)
                        turns_by_index[index] = turn
                    turn.raw_tool_output_chars = max(
                        turn.raw_tool_output_chars,
                        int(raw_fact.get("raw_tool_output_chars", 0) or 0)
                        if isinstance(raw_fact.get("raw_tool_output_chars", 0), int)
                        else 0,
                    )
                    facts = raw_fact.get("snr_facts", {})
                    if isinstance(facts, Mapping) and not turn.snr_facts:
                        turn.snr_facts = {
                            str(key): int(value)
                            for key, value in facts.items()
                            if isinstance(value, int) and not isinstance(value, bool) and value >= 0
                        }
                    for field_name in raw_fact.get("context_fields", []):
                        if isinstance(field_name, str):
                            turn.context_meta[field_name] = True
                    context_presence = raw_fact.get("context_presence", {})
                    if isinstance(context_presence, Mapping):
                        for field_name, value in context_presence.items():
                            if isinstance(field_name, str) and isinstance(value, bool):
                                turn.context_meta[field_name] = value
                    context_values = raw_fact.get("context_values", {})
                    if isinstance(context_values, Mapping):
                        for field_name, value in context_values.items():
                            if field_name in {"task_ref", "session_ref", "continuity_event"} and isinstance(value, str):
                                turn.context_meta[str(field_name)] = value
                    known_event_types = {
                        str(event.get("type", ""))
                        for event in turn.events
                        if isinstance(event, Mapping)
                    }
                    for event_type in raw_fact.get("event_types", []):
                        if isinstance(event_type, str) and event_type not in known_event_types:
                            turn.events.append({"type": event_type, "status": "unknown"})
                            known_event_types.add(event_type)

            raw_fact_calls = metric_facts.get("calls", [])
            if isinstance(raw_fact_calls, list):
                for raw_fact in raw_fact_calls:
                    if not isinstance(raw_fact, Mapping):
                        continue
                    try:
                        turn_index = int(raw_fact.get("turn_index", 0))
                    except (TypeError, ValueError):
                        continue
                    turn = turns_by_index.get(turn_index)
                    if turn is None:
                        continue
                    call_id = str(raw_fact.get("call_id", ""))
                    name = str(raw_fact.get("name", "unknown"))
                    existing = next(
                        (
                            call
                            for call in turn.tool_calls
                            if call.call_id == call_id and call.name == name
                        ),
                        None,
                    )
                    if existing is not None:
                        if not existing.argument_fingerprint:
                            existing.argument_fingerprint = str(raw_fact.get("argument_fingerprint", ""))
                        if not existing.command_fingerprint:
                            existing.command_fingerprint = str(raw_fact.get("command_fingerprint", ""))
                        if not existing.arguments and isinstance(raw_fact.get("arguments", {}), Mapping):
                            existing.arguments = dict(raw_fact["arguments"])
                        if existing.success is None and isinstance(raw_fact.get("success"), bool):
                            existing.success = raw_fact["success"]
                        if existing.exit_code is None and isinstance(raw_fact.get("exit_code"), int) and not isinstance(raw_fact.get("exit_code"), bool):
                            existing.exit_code = raw_fact["exit_code"]
                        if existing.status == "unknown":
                            existing.status = str(raw_fact.get("status", "unknown"))
                        if not existing.result_metadata and isinstance(raw_fact.get("result_metadata", {}), Mapping):
                            existing.result_metadata = dict(raw_fact["result_metadata"])
                        existing.__post_init__()
                        continue
                    status = str(raw_fact.get("status", "unknown"))
                    call = ToolCall(
                        name=name,
                        arguments=dict(raw_fact.get("arguments", {})) if isinstance(raw_fact.get("arguments", {}), Mapping) else {},
                        argument_fingerprint=str(raw_fact.get("argument_fingerprint", "")),
                        command_fingerprint=str(raw_fact.get("command_fingerprint", "")),
                        call_id=call_id,
                        raw_call_id=str(raw_fact.get("raw_call_id", call_id)),
                        success=raw_fact.get("success") if isinstance(raw_fact.get("success"), bool) else None,
                        exit_code=raw_fact.get("exit_code") if isinstance(raw_fact.get("exit_code"), int) and not isinstance(raw_fact.get("exit_code"), bool) else None,
                        status=status,
                        result_metadata=dict(raw_fact.get("result_metadata", {})) if isinstance(raw_fact.get("result_metadata", {}), Mapping) else {},
                    )
                    turn.tool_calls.append(call)
            session.turns.sort(key=lambda turn: turn.index)
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
    path_part = normalized.split("#", 1)[0]
    if (
        normalized.startswith("/")
        or normalized.startswith("//")
        or re.match(r"^[A-Za-z]:/", normalized)
        or ".." in Path(path_part).parts
    ):
        raise BundleError("bundle source ref must be relative")


def _validate_argument_fingerprint(value: Any) -> None:
    if value in (None, ""):
        return
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise BundleError("bundle argument fingerprint must be a SHA-256 hex string")


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
