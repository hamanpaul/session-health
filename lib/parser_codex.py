"""Codex CLI JSONL session parser.

Parses session logs from ~/.codex/sessions/{YYYY}/{MM}/{session}.jsonl
Record types: session_meta, response_item, event_msg, function_call,
              function_call_output, reasoning, state, turn_context, compacted
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .parser_base import Session, Turn, ToolCall


def parse_codex_session(path: str | Path) -> Session:
    """Parse a Codex CLI JSONL session file into a Session object."""
    path = Path(path)
    records: list[dict] = []
    diagnostics: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line_number, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    record["__source_line__"] = line_number
                    records.append(record)
                else:
                    diagnostics.append({
                        "kind": "non_object_record",
                        "line": line_number,
                        "status": "unknown",
                    })
            except json.JSONDecodeError:
                diagnostics.append({
                    "kind": "malformed_record",
                    "line": line_number,
                    "status": "unknown",
                })

    session = Session(
        id="",
        source="codex",
        diagnostics=diagnostics,
        parser_version="codex-2",
        source_ref=path.name,
        source_capabilities={
            "format": "codex-jsonl",
            "supports_nested_response_items": True,
            "supports_call_result_pairing": True,
            "outcome_states": ["success", "failed", "unknown"],
        },
    )

    # --- Extract session metadata ---
    for rec in records:
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if rtype == "session_meta" and not isinstance(payload, dict):
            continue

        if rtype == "session_meta":
            session.id = payload.get("id", "")
            session.cwd = payload.get("cwd", "")
            session.cli_version = payload.get("cli_version", "")
            session.timestamp_start = rec.get("timestamp", "")
            model_provider = payload.get("model_provider", "")
            session.model = payload.get("model", model_provider)
            session.metadata = payload
            break

    # Track last timestamp for duration
    if records:
        session.timestamp_end = records[-1].get("timestamp", "")

    # --- Build turns from response_item records ---
    # Strategy: group consecutive records into turns.
    # A new turn starts when we see a user-role response_item.

    turns: List[Turn] = []
    current_turn: Turn | None = None
    turn_idx = 0
    pending_calls: dict[str, ToolCall] = {}  # call_id → ToolCall
    pending_results: dict[str, Tuple[str, Dict[str, Any]]] = {}
    resolved_calls: set[str] = set()

    def record_line(record: Dict[str, Any]) -> int:
        return int(record.get("__source_line__", 0) or 0)

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

    def decode_arguments(raw: Any, line_number: int) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
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

    def attach_result(call: ToolCall, output: Any, result_meta: Dict[str, Any]) -> None:
        if isinstance(output, (dict, list)):
            output = json.dumps(output, ensure_ascii=False)
        call.output = "" if output is None else str(output)
        if isinstance(result_meta.get("exit_code"), int):
            call.exit_code = result_meta["exit_code"]
        success = result_meta.get("success")
        if isinstance(success, bool):
            call.success = success
        elif call.exit_code is not None:
            call.success = call.exit_code == 0
        call.__post_init__()
        resolved_calls.add(call.call_id)

    for rec in records:
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            add_diagnostic("malformed_payload", record_line(rec))
            continue
        ts = rec.get("timestamp", "")

        # Newer Codex records wrap tool calls/results in response_item.payload.
        nested_type = payload.get("type", "") if rtype == "response_item" else ""
        effective_type = nested_type or rtype

        if rtype == "response_item" and nested_type not in ("function_call", "function_call_output"):
            role = payload.get("role", "")
            content_parts = payload.get("content", [])
            text = _extract_text(content_parts) if isinstance(content_parts, list) else str(content_parts)

            if role == "user":
                # Start a new turn
                if current_turn is not None:
                    turns.append(current_turn)
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=ts)
                current_turn.user_input = text

            elif role == "assistant":
                if current_turn is None:
                    current_turn = Turn(index=turn_idx + 1, timestamp=ts)
                    turn_idx += 1
                current_turn.assistant_output += text

            elif role == "developer":
                # Developer messages contain context info (system prompt, env)
                if current_turn is None:
                    current_turn = Turn(index=turn_idx + 1, timestamp=ts)
                    turn_idx += 1
                current_turn.total_context_chars += len(text)
                _extract_context_meta(text, current_turn)

        elif effective_type == "function_call":
            call_id = payload.get("call_id", payload.get("id", ""))
            name = payload.get("name", "")
            args = payload.get("arguments", {})
            turn = current_or_new_turn(ts)
            if not call_id:
                add_diagnostic("missing_call_id", record_line(rec))
                call_id = f"unknown-call-{len(turn.tool_calls) + 1}"
            if call_id in pending_calls:
                add_diagnostic("duplicate_call_id", record_line(rec), call_id=call_id)
                call_id = f"{call_id}#duplicate-{len(turn.tool_calls) + 1}"
            tc = ToolCall(
                name=str(name or "unknown"),
                arguments=decode_arguments(args, record_line(rec)),
                call_id=str(call_id),
                source_ref=f"{path.name}#L{record_line(rec)}",
            )
            pending_calls[call_id] = tc
            turn.tool_calls.append(tc)
            if call_id in pending_results:
                output, result_meta = pending_results.pop(call_id)
                attach_result(tc, output, result_meta)

        elif effective_type == "function_call_output":
            call_id = payload.get("call_id", payload.get("id", ""))
            output = payload.get("output", "")
            result_meta = {
                "success": payload.get("success"),
                "exit_code": payload.get("exit_code", payload.get("exitCode")),
            }
            if call_id in pending_calls:
                attach_result(pending_calls[call_id], output, result_meta)
            else:
                if isinstance(output, (dict, list)):
                    output = json.dumps(output, ensure_ascii=False)
                pending_results[call_id] = ("" if output is None else str(output), result_meta)
            if current_turn is not None:
                output_text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
                current_turn.raw_tool_output_chars += len(output_text or "")

        elif rtype == "event_msg":
            event_type = payload.get("type", "")
            turn = current_or_new_turn(ts)
            turn.events.append(payload)
            # Track session-level counters
            if event_type == "context_compacted":
                session.context_compacted_count += 1
            elif event_type == "task_started":
                session.task_started_count += 1
            elif event_type == "task_complete":
                session.task_complete_count += 1
            elif event_type == "turn_aborted":
                session.turn_aborted_count += 1

        elif rtype == "turn_context":
            turn = current_or_new_turn(ts)
            ctx_text = _extract_text(payload.get("content", []))
            turn.total_context_chars += len(ctx_text)

        elif rtype not in ("session_meta",):
            turn = current_or_new_turn(ts)
            turn.events.append({"type": effective_type or "unknown", "status": "unknown", "raw": payload})
            add_diagnostic("unknown_record", record_line(rec), record_type=rtype)

    # Don't forget the last turn
    if current_turn is not None:
        turns.append(current_turn)

    session.turns = turns
    for call_id in pending_results:
        if call_id not in pending_calls:
            session.diagnostics.append({
                "kind": "orphan_call_result",
                "call_id": call_id,
                "status": "unknown",
            })
    for call_id in pending_calls:
        if call_id not in resolved_calls:
            session.diagnostics.append({
                "kind": "missing_call_result",
                "call_id": call_id,
                "status": "unknown",
            })
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
    """Try to extract environment context from developer messages."""
    lower = text.lower()
    if "current working directory" in lower or "cwd:" in lower:
        turn.context_meta["cwd_present"] = True
    if "exit code" in lower or "exit_code" in lower or "exited with" in lower:
        turn.context_meta["exit_code_present"] = True
    if "permission" in lower or "sandbox" in lower:
        turn.context_meta["permission_present"] = True
    if "git" in lower and ("branch" in lower or "status" in lower or "repository" in lower):
        turn.context_meta["git_present"] = True
