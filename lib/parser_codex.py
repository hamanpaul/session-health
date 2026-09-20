"""Codex CLI JSONL session parser.

The adapter keeps source order and source-line references in ``Session.event_log``.
Call IDs are only a pairing hint: a repeated ID is ambiguous and is never used
to overwrite an earlier result.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .parser_base import Session, SessionInputLimits, ToolCall, Turn, read_jsonl_records, source_ref_line


def parse_codex_session(
    path: str | Path,
    *,
    input_limits: SessionInputLimits | None = None,
) -> Session:
    """Parse a bounded Codex CLI JSONL session into a normalized session."""

    path = Path(path)
    input_limits = input_limits or SessionInputLimits()
    records, diagnostics = read_jsonl_records(path, input_limits)
    session = Session(
        id="",
        source="codex",
        diagnostics=diagnostics,
        parser_version="codex-3",
        source_ref=path.name,
        source_capabilities={
            "format": "codex-jsonl",
            "supports_nested_response_items": True,
            "supports_call_result_pairing": True,
            "supports_structured_exit_code": True,
            "outcome_states": ["success", "failed", "unknown"],
            "input_limits": {
                "max_bytes": input_limits.max_bytes,
                "max_records": input_limits.max_records,
                "max_record_chars": input_limits.max_record_chars,
            },
        },
    )

    for record in records:
        if record.get("type") != "session_meta":
            continue
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            continue
        session.id = str(payload.get("id", "") or "")
        session.cwd = str(payload.get("cwd", "") or "")
        session.cli_version = str(payload.get("cli_version", "") or "")
        session.timestamp_start = str(record.get("timestamp", "") or "")
        model_provider = payload.get("model_provider", "")
        session.model = str(payload.get("model", model_provider) or "")
        session.metadata = dict(payload)
        break
    if records:
        session.timestamp_end = str(records[-1].get("timestamp", "") or "")

    turns: List[Turn] = []
    current_turn: Turn | None = None
    turn_idx = 0
    pending_calls: dict[str, list[ToolCall]] = {}
    pending_results: dict[str, list[dict[str, Any]]] = {}
    resolved_calls: set[int] = set()
    occurrences: dict[str, int] = {}
    ambiguous_ids: set[str] = set()
    call_turns: dict[int, int] = {}

    def record_line(record: Dict[str, Any]) -> int:
        return int(record.get("__source_line__", 0) or 0)

    def record_sequence(record: Dict[str, Any]) -> int:
        return int(record.get("__record_sequence__", record_line(record)) or 0)

    def source_ref(record: Dict[str, Any]) -> str:
        return f"{path.name}#L{record_line(record)}"

    def current_or_new_turn(timestamp: str) -> Turn:
        nonlocal current_turn, turn_idx
        if current_turn is None:
            turn_idx += 1
            current_turn = Turn(index=turn_idx, timestamp=timestamp)
        return current_turn

    def add_diagnostic(kind: str, line_number: int, **extra: Any) -> None:
        diagnostic = {"kind": kind, "line": line_number, "status": "unknown"}
        diagnostic.update(extra)
        session.diagnostics.append(diagnostic)
        if current_turn is not None:
            current_turn.diagnostics.append(diagnostic)

    def append_log(
        kind: str,
        record: Dict[str, Any],
        payload: Dict[str, Any],
        turn: Turn | None,
        ref: str | None = None,
    ) -> None:
        session.event_log.append(
            {
                "sequence": record_sequence(record),
                "kind": kind,
                "turn_index": turn.index if turn is not None else None,
                "timestamp": str(record.get("timestamp", "") or ""),
                "source_ref": ref or source_ref(record),
                "payload": payload,
            }
        )

    def decode_arguments(raw: Any, line_number: int) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                add_diagnostic("malformed_arguments", line_number)
                return {"raw": raw}
            if isinstance(value, dict):
                return value
            add_diagnostic("non_object_arguments", line_number)
            return {"raw": value}
        if raw is None:
            add_diagnostic("missing_arguments", line_number)
            return {}
        add_diagnostic("non_object_arguments", line_number)
        return {"raw": raw}

    def result_metadata(payload: Dict[str, Any]) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key in (
            "cwd",
            "working_directory",
            "workingDirectory",
            "permission",
            "permissions",
            "git",
            "git_status",
            "task_id",
            "taskId",
            "session_id",
            "sessionId",
        ):
            if key in payload:
                result[key] = payload[key]
        return result

    def apply_structured_context(turn: Turn, result: Dict[str, Any]) -> None:
        if any(key in result for key in ("cwd", "working_directory", "workingDirectory")):
            turn.context_meta["cwd_present"] = True
        if "exit_code" in result or "exitCode" in result:
            exit_code = result.get("exit_code", result.get("exitCode"))
            if isinstance(exit_code, int) and not isinstance(exit_code, bool):
                turn.context_meta["exit_code_present"] = True
        if "permission" in result or "permissions" in result:
            turn.context_meta["permission_present"] = True
        if "git" in result or "git_status" in result:
            turn.context_meta["git_present"] = True

    def attach_result(
        call: ToolCall,
        output: Any,
        result: Dict[str, Any],
        record: Dict[str, Any],
    ) -> None:
        if isinstance(output, (dict, list)):
            output = json.dumps(output, ensure_ascii=False)
        call.output = "" if output is None else str(output)
        exit_code = result.get("exit_code", result.get("exitCode"))
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            call.exit_code = exit_code
        success = result.get("success")
        if isinstance(success, bool):
            call.success = success
        elif call.exit_code is not None:
            call.success = call.exit_code == 0
        call.result_source_ref = source_ref(record)
        call.result_timestamp = str(record.get("timestamp", "") or "")
        call.result_sequence = record_sequence(record)
        call.result_metadata = result_metadata(result)
        call.__post_init__()
        resolved_calls.add(id(call))

    def ambiguous_result(raw_id: str, record: Dict[str, Any]) -> None:
        add_diagnostic(
            "ambiguous_call_result",
            record_line(record),
            call_id=raw_id,
            source_ref=source_ref(record),
            status="unknown",
        )
        for call in pending_calls.get(raw_id, []):
            if "ambiguous_call_result" not in call.diagnostics:
                call.diagnostics.append("ambiguous_call_result")

    def attach_pending_result(raw_id: str, turn: Turn, record: Dict[str, Any]) -> None:
        values = pending_results.get(raw_id, [])
        unresolved = [
            call for call in pending_calls.get(raw_id, []) if id(call) not in resolved_calls
        ]
        if raw_id in ambiguous_ids or len(unresolved) != 1 or len(values) != 1:
            ambiguous_result(raw_id, record)
            return
        result = values.pop(0)
        attach_result(unresolved[0], result["output"], result["meta"], result["record"])
        if not values:
            pending_results.pop(raw_id, None)
        apply_structured_context(turn, result["meta"])

    for record in records:
        rtype = record.get("type", "")
        payload = record.get("payload", {})
        if not isinstance(payload, dict):
            add_diagnostic("malformed_payload", record_line(record))
            continue
        timestamp = str(record.get("timestamp", "") or "")
        nested_type = payload.get("type", "") if rtype == "response_item" else ""
        effective_type = nested_type or rtype

        if rtype == "response_item" and nested_type not in ("function_call", "function_call_output"):
            role = str(payload.get("role", "") or "")
            content = payload.get("content", [])
            text = _extract_text(content) if isinstance(content, list) else str(content)
            if role == "user":
                if current_turn is not None:
                    turns.append(current_turn)
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=timestamp)
                current_turn.user_input = text
                append_log("user_message", record, {"text": text}, current_turn)
            elif role == "assistant":
                turn = current_or_new_turn(timestamp)
                turn.assistant_output += text
                append_log("assistant_message", record, {"text": text}, turn)
            elif role == "developer":
                turn = current_or_new_turn(timestamp)
                turn.total_context_chars += len(text)
                _extract_context_meta(text, turn)
                append_log(
                    "context_metadata",
                    record,
                    {"role": "developer", "fields": dict(turn.context_meta)},
                    turn,
                )
            else:
                turn = current_or_new_turn(timestamp)
                turn.events.append({"type": effective_type or "unknown", "status": "unknown"})
                append_log(
                    "unknown_event",
                    record,
                    {"type": effective_type or "unknown", "status": "unknown"},
                    turn,
                )

        elif effective_type == "function_call":
            raw_id = str(payload.get("call_id", payload.get("id", "")) or "")
            turn = current_or_new_turn(timestamp)
            if not raw_id:
                add_diagnostic("missing_call_id", record_line(record))
                raw_id = f"unknown-call-{len(turn.tool_calls) + 1}"
            occurrences[raw_id] = occurrences.get(raw_id, 0) + 1
            if occurrences[raw_id] > 1:
                ambiguous_ids.add(raw_id)
                add_diagnostic("duplicate_call_id", record_line(record), call_id=raw_id)
                call_id = f"{raw_id}#duplicate-{occurrences[raw_id]}"
            else:
                call_id = raw_id
            call = ToolCall(
                name=str(payload.get("name", "") or "unknown"),
                arguments=decode_arguments(payload.get("arguments", {}), record_line(record)),
                call_id=call_id,
                raw_call_id=raw_id,
                source_ref=source_ref(record),
                timestamp=timestamp,
                sequence=record_sequence(record),
            )
            pending_calls.setdefault(raw_id, []).append(call)
            call_turns[id(call)] = turn.index
            turn.tool_calls.append(call)
            append_log(
                "tool_call",
                record,
                {
                    "call_id": raw_id,
                    "name": call.name,
                    "arguments": call.arguments,
                    "status": call.status,
                },
                turn,
                call.source_ref,
            )
            if raw_id in pending_results:
                attach_pending_result(raw_id, turn, record)

        elif effective_type == "function_call_output":
            raw_id = str(payload.get("call_id", payload.get("id", "")) or "")
            output = payload.get("output", "")
            meta = {
                "success": payload.get("success"),
                "exit_code": payload.get("exit_code", payload.get("exitCode")),
            }
            meta.update(result_metadata(payload))
            turn = current_or_new_turn(timestamp)
            call_bucket = pending_calls.get(raw_id, [])
            if call_bucket:
                expected_index = call_turns.get(id(call_bucket[0]))
                for candidate in turns + ([current_turn] if current_turn else []):
                    if candidate is not None and candidate.index == expected_index:
                        turn = candidate
                        break
            if raw_id in pending_calls:
                unresolved = [
                    call for call in pending_calls[raw_id] if id(call) not in resolved_calls
                ]
                if raw_id in ambiguous_ids or len(unresolved) != 1:
                    ambiguous_result(raw_id, record)
                else:
                    attach_result(unresolved[0], output, meta, record)
                    apply_structured_context(turn, meta)
            else:
                pending_results.setdefault(raw_id, []).append(
                    {"output": output, "meta": meta, "record": record}
                )
            if isinstance(output, (dict, list)):
                output_text = json.dumps(output, ensure_ascii=False)
            else:
                output_text = "" if output is None else str(output)
            turn.raw_tool_output_chars += len(output_text)
            append_log(
                "tool_result",
                record,
                {
                    "call_id": raw_id,
                    "output": output,
                    "success": meta.get("success"),
                    "exit_code": meta.get("exit_code"),
                    "result_metadata": result_metadata(meta),
                },
                turn,
                source_ref(record),
            )

        elif rtype == "event_msg":
            event_type = str(payload.get("type", "") or "")
            turn = current_or_new_turn(timestamp)
            turn.events.append(dict(payload))
            if event_type == "context_compacted":
                session.context_compacted_count += 1
            elif event_type == "task_started":
                session.task_started_count += 1
            elif event_type == "task_complete":
                session.task_complete_count += 1
            elif event_type == "turn_aborted":
                session.turn_aborted_count += 1
            _extract_lifecycle_context(payload, turn)
            append_log("session_event", record, dict(payload), turn)

        elif rtype == "turn_context":
            turn = current_or_new_turn(timestamp)
            ctx_text = _extract_text(payload.get("content", []))
            turn.total_context_chars += len(ctx_text)
            append_log("context_metadata", record, {"fields": dict(turn.context_meta)}, turn)

        elif rtype != "session_meta":
            turn = current_or_new_turn(timestamp)
            diagnostic_event = {"type": effective_type or "unknown", "status": "unknown"}
            turn.events.append(diagnostic_event)
            append_log("unknown_event", record, diagnostic_event, turn)
            add_diagnostic("unknown_record", record_line(record), record_type=rtype)

    if current_turn is not None:
        turns.append(current_turn)
    session.turns = turns

    for raw_id, values in pending_results.items():
        if raw_id in ambiguous_ids:
            for value in values:
                ambiguous_result(raw_id, value["record"])
        else:
            for value in values:
                session.diagnostics.append(
                    {
                        "kind": "orphan_call_result",
                        "call_id": raw_id,
                        "line": record_line(value["record"]),
                        "source_ref": source_ref(value["record"]),
                        "status": "unknown",
                    }
                )
    for raw_id, calls in pending_calls.items():
        for call in calls:
            if id(call) not in resolved_calls:
                session.diagnostics.append(
                    {
                        "kind": "missing_call_result",
                        "call_id": call.call_id,
                        "raw_call_id": raw_id,
                        "line": source_ref_line(call.source_ref),
                        "source_ref": call.source_ref,
                        "status": "unknown",
                    }
                )
    session.event_log.sort(key=lambda event: int(event.get("sequence", 0)))
    return session


def _extract_text(content_parts: list) -> str:
    """Extract text from content parts array."""
    texts = []
    for part in content_parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            value = part.get("text", part.get("input_text", ""))
            texts.append(str(value) if value is not None else "")
        else:
            texts.append(str(part))
    return "\n".join(texts)


def _extract_context_meta(text: str, turn: Turn) -> None:
    """Extract only typed environment-presence flags from developer text."""
    lower = text.lower()
    if "current working directory" in lower or "cwd:" in lower:
        turn.context_meta["cwd_present"] = True
    if "exit code" in lower or "exit_code" in lower or "exited with" in lower:
        turn.context_meta["exit_code_present"] = True
    if "permission" in lower or "sandbox" in lower:
        turn.context_meta["permission_present"] = True
    if "git" in lower and ("branch" in lower or "status" in lower or "repository" in lower):
        turn.context_meta["git_present"] = True


def _extract_lifecycle_context(payload: Dict[str, Any], turn: Turn) -> None:
    event_type = str(payload.get("type", "") or "")
    ref = payload.get("task_ref", payload.get("task_id", payload.get("taskId")))
    if ref:
        turn.context_meta["task_ref"] = str(ref)
    if event_type in {"task_continued", "context_restored"}:
        turn.context_meta["continuity_event"] = event_type
