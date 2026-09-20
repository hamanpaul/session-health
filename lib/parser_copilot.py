"""Copilot CLI JSONL session parser with bounded, ordered pairing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .parser_base import Session, ToolCall, Turn, read_jsonl_records


def parse_copilot_session(path: str | Path) -> Session:
    """Parse a bounded Copilot CLI JSONL session into a normalized session."""

    path = Path(path)
    records, diagnostics = read_jsonl_records(path)
    session = Session(
        id="",
        source="copilot",
        diagnostics=diagnostics,
        parser_version="copilot-3",
        source_ref=path.name,
        source_capabilities={
            "format": "copilot-jsonl",
            "supports_json_string_arguments": True,
            "supports_call_result_pairing": True,
            "supports_structured_exit_code": True,
            "outcome_states": ["success", "failed", "unknown"],
        },
    )

    for record in records:
        if record.get("type") != "session.start":
            continue
        data = record.get("data", {})
        if not isinstance(data, dict):
            continue
        session.id = str(data.get("sessionId", "") or "")
        session.cwd = str(data.get("cwd", data.get("workingDirectory", "")) or "")
        session.cli_version = str(data.get("copilotVersion", "") or "")
        session.timestamp_start = str(record.get("timestamp", "") or "")
        session.metadata = dict(data)
        break
    for record in records:
        if record.get("type") == "session.model_change":
            data = record.get("data", {})
            if isinstance(data, dict):
                session.model = str(data.get("newModel", "") or "")
    if records:
        session.timestamp_end = str(records[-1].get("timestamp", "") or "")

    turns: List[Turn] = []
    current_turn: Turn | None = None
    turn_idx = 0
    pending_tools: dict[str, list[ToolCall]] = {}
    pending_results: dict[str, list[dict[str, Any]]] = {}
    resolved_tools: set[int] = set()
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

    def result_metadata(data: Dict[str, Any]) -> Dict[str, Any]:
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
            if key in data:
                result[key] = data[key]
        return result

    def apply_structured_context(turn: Turn, result: Dict[str, Any]) -> None:
        if any(key in result for key in ("cwd", "working_directory", "workingDirectory")):
            turn.context_meta["cwd_present"] = True
        if "exit_code" in result or "exitCode" in result:
            if isinstance(result.get("exit_code", result.get("exitCode")), int):
                turn.context_meta["exit_code_present"] = True
        if "permission" in result or "permissions" in result:
            turn.context_meta["permission_present"] = True
        if "git" in result or "git_status" in result:
            turn.context_meta["git_present"] = True

    def attach_result(
        call: ToolCall,
        output: str,
        success: Any,
        exit_code: Any,
        result: Dict[str, Any],
        record: Dict[str, Any],
    ) -> None:
        call.output = output
        call.success = success if isinstance(success, bool) else None
        if isinstance(exit_code, int) and not isinstance(exit_code, bool):
            call.exit_code = exit_code
        call.result_source_ref = source_ref(record)
        call.result_timestamp = str(record.get("timestamp", "") or "")
        call.result_sequence = record_sequence(record)
        call.result_metadata = result_metadata(result)
        call.__post_init__()
        resolved_tools.add(id(call))

    def ambiguous_result(raw_id: str, record: Dict[str, Any]) -> None:
        add_diagnostic(
            "ambiguous_call_result",
            record_line(record),
            call_id=raw_id,
            source_ref=source_ref(record),
            status="unknown",
        )
        for call in pending_tools.get(raw_id, []):
            if "ambiguous_call_result" not in call.diagnostics:
                call.diagnostics.append("ambiguous_call_result")

    def attach_pending_result(raw_id: str, turn: Turn, record: Dict[str, Any]) -> None:
        values = pending_results.get(raw_id, [])
        unresolved = [
            call for call in pending_tools.get(raw_id, []) if id(call) not in resolved_tools
        ]
        if raw_id in ambiguous_ids or len(unresolved) != 1 or len(values) != 1:
            ambiguous_result(raw_id, record)
            return
        value = values.pop(0)
        attach_result(
            unresolved[0],
            value["output"],
            value["success"],
            value["exit_code"],
            value["meta"],
            value["record"],
        )
        if not values:
            pending_results.pop(raw_id, None)
        apply_structured_context(turn, value["meta"])

    for record in records:
        etype = record.get("type", "")
        data = record.get("data", {})
        if not isinstance(data, dict):
            add_diagnostic("malformed_payload", record_line(record))
            data = {}
        timestamp = str(record.get("timestamp", "") or "")

        if etype == "user.message":
            if current_turn is not None:
                turns.append(current_turn)
            turn_idx += 1
            current_turn = Turn(index=turn_idx, timestamp=timestamp)
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text", str(part))) if isinstance(part, dict) else str(part)
                    for part in content
                )
            elif not isinstance(content, str):
                content = str(content)
            current_turn.user_input = content
            append_log("user_message", record, {"text": content}, current_turn)

        elif etype == "assistant.message":
            turn = current_or_new_turn(timestamp)
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(part.get("text", str(part))) if isinstance(part, dict) else str(part)
                    for part in content
                )
            elif not isinstance(content, str):
                content = str(content)
            turn.assistant_output += content
            append_log("assistant_message", record, {"text": content}, turn)

        elif etype in {"assistant.turn_start", "assistant.turn_end"}:
            append_log("lifecycle", record, {"type": etype, "status": "observed"}, current_turn)

        elif etype == "tool.execution_start":
            turn = current_or_new_turn(timestamp)
            raw_id = str(data.get("toolCallId", "") or "")
            if not raw_id:
                add_diagnostic("missing_call_id", record_line(record))
                raw_id = f"unknown-tool-{len(turn.tool_calls) + 1}"
            occurrences[raw_id] = occurrences.get(raw_id, 0) + 1
            if occurrences[raw_id] > 1:
                ambiguous_ids.add(raw_id)
                add_diagnostic("duplicate_call_id", record_line(record), call_id=raw_id)
                call_id = f"{raw_id}#duplicate-{occurrences[raw_id]}"
            else:
                call_id = raw_id
            call = ToolCall(
                name=str(data.get("toolName", "") or "unknown"),
                arguments=decode_arguments(data.get("arguments", {}), record_line(record)),
                call_id=call_id,
                raw_call_id=raw_id,
                source_ref=source_ref(record),
                timestamp=timestamp,
                sequence=record_sequence(record),
            )
            pending_tools.setdefault(raw_id, []).append(call)
            call_turns[id(call)] = turn.index
            turn.tool_calls.append(call)
            if call.name in {"bash", "shell", "exec_command"}:
                cmd = call.arguments.get("command", call.arguments.get("cmd", ""))
                if cmd:
                    turn.context_meta.setdefault("has_shell", True)
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

        elif etype == "tool.execution_complete":
            raw_id = str(data.get("toolCallId", "") or "")
            success = data.get("success", None)
            result = data.get("result", {})
            output = ""
            result_meta: Dict[str, Any] = {}
            if isinstance(result, dict):
                output = result.get("content", result.get("output", ""))
                result_meta.update(result_metadata(result))
                if isinstance(output, (list, dict)):
                    output = json.dumps(output, ensure_ascii=False)
            elif isinstance(result, str):
                output = result
            else:
                output = "" if output is None else str(output)
            exit_code = data.get("exitCode", data.get("exit_code"))
            result_meta.update(result_metadata(data))
            result_meta["exitCode"] = exit_code
            turn = current_or_new_turn(timestamp)
            bucket = pending_tools.get(raw_id, [])
            if bucket:
                expected_index = call_turns.get(id(bucket[0]))
                for candidate in turns + ([current_turn] if current_turn else []):
                    if candidate is not None and candidate.index == expected_index:
                        turn = candidate
                        break
            if raw_id in pending_tools:
                unresolved = [
                    call for call in pending_tools[raw_id] if id(call) not in resolved_tools
                ]
                if raw_id in ambiguous_ids or len(unresolved) != 1:
                    ambiguous_result(raw_id, record)
                else:
                    attach_result(
                        unresolved[0],
                        str(output or ""),
                        success,
                        exit_code,
                        result_meta,
                        record,
                    )
                    apply_structured_context(turn, result_meta)
            else:
                pending_results.setdefault(raw_id, []).append(
                    {
                        "output": str(output or ""),
                        "success": success,
                        "exit_code": exit_code,
                        "meta": result_meta,
                        "record": record,
                    }
                )
            turn.raw_tool_output_chars += len(str(output or ""))
            _extract_copilot_context(output, turn, result_meta)
            append_log(
                "tool_result",
                record,
                {
                    "call_id": raw_id,
                    "output": output,
                    "success": success,
                    "exit_code": exit_code,
                    "result_metadata": result_metadata(result_meta),
                },
                turn,
                source_ref(record),
            )

        elif etype == "session.truncation":
            session.context_compacted_count += 1
            turn = current_or_new_turn(timestamp)
            turn.events.append({"type": "context_compacted"})
            append_log("session_event", record, {"type": "context_compacted"}, turn)

        elif etype not in {"session.start", "session.model_change"}:
            turn = current_or_new_turn(timestamp)
            unknown = {"type": etype or "unknown", "status": "unknown"}
            turn.events.append(unknown)
            append_log("unknown_event", record, unknown, turn)
            add_diagnostic("unknown_record", record_line(record), record_type=etype)

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
    for raw_id, calls in pending_tools.items():
        for call in calls:
            if id(call) not in resolved_tools:
                session.diagnostics.append(
                    {
                        "kind": "missing_call_result",
                        "call_id": call.call_id,
                        "raw_call_id": raw_id,
                        "line": call.source_ref,
                        "status": "unknown",
                    }
                )
    session.event_log.sort(key=lambda event: int(event.get("sequence", 0)))
    return session


def _extract_copilot_context(
    output: str,
    turn: Turn,
    result_metadata: Dict[str, Any] | None = None,
) -> None:
    """Extract typed state-presence flags, including structured exitCode."""
    result_metadata = result_metadata or {}
    if any(key in result_metadata for key in ("cwd", "working_directory", "workingDirectory")):
        turn.context_meta["cwd_present"] = True
    if "exitCode" in result_metadata or "exit_code" in result_metadata:
        if isinstance(result_metadata.get("exitCode", result_metadata.get("exit_code")), int):
            turn.context_meta["exit_code_present"] = True
    if "permission" in result_metadata or "permissions" in result_metadata:
        turn.context_meta["permission_present"] = True
    if "git" in result_metadata or "git_status" in result_metadata:
        turn.context_meta["git_present"] = True
    if not output:
        return
    lower = str(output).lower()
    if any(kw in lower for kw in ("pwd", "current working directory", "/home/")):
        turn.context_meta["cwd_present"] = True
    if any(kw in lower for kw in ("exit code", "exited with exit code", "exit_code")):
        turn.context_meta["exit_code_present"] = True
    if "permission" in lower or "denied" in lower:
        turn.context_meta["permission_present"] = True
    if "git" in lower and any(kw in lower for kw in ("branch", "status", "commit", "repository")):
        turn.context_meta["git_present"] = True
