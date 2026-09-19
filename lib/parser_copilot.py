"""Copilot CLI JSONL session parser.

Parses session logs from ~/.copilot/session-state/{uuid}.jsonl
Event types: session.start, session.model_change, user.message,
             assistant.message, assistant.turn_start, assistant.turn_end,
             tool.execution_start, tool.execution_complete, session.truncation
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .parser_base import Session, Turn, ToolCall


def parse_copilot_session(path: str | Path) -> Session:
    """Parse a Copilot CLI JSONL session file into a Session object."""
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
        source="copilot",
        diagnostics=diagnostics,
        parser_version="copilot-2",
        source_ref=path.name,
        source_capabilities={
            "format": "copilot-jsonl",
            "supports_json_string_arguments": True,
            "supports_call_result_pairing": True,
            "outcome_states": ["success", "failed", "unknown"],
        },
    )

    # --- Extract session metadata ---
    for rec in records:
        if rec.get("type") == "session.start":
            data = rec.get("data", {})
            session.id = data.get("sessionId", "")
            session.cli_version = data.get("copilotVersion", "")
            session.timestamp_start = rec.get("timestamp", "")
            session.metadata = data
            break

    # Model
    for rec in records:
        if rec.get("type") == "session.model_change":
            session.model = rec.get("data", {}).get("newModel", "")

    # Last timestamp
    if records:
        session.timestamp_end = records[-1].get("timestamp", "")

    # --- Build turns ---
    # Copilot CLI uses assistant.turn_start / turn_end to delimit turns.
    # Within each turn: user.message → tool.execution_* → assistant.message

    turns: List[Turn] = []
    current_turn: Turn | None = None
    turn_idx = 0
    pending_tools: dict[str, ToolCall] = {}  # toolCallId → ToolCall
    pending_results: dict[str, tuple[str, Any, Any]] = {}
    resolved_tools: set[str] = set()
    orphan_results: set[str] = set()

    def record_line(record: Dict[str, Any]) -> int:
        return int(record.get("__source_line__", 0) or 0)

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

    def attach_result(call: ToolCall, output: str, success: Any, exit_code: Any) -> None:
        call.output = output
        call.success = success if isinstance(success, bool) else None
        if isinstance(exit_code, int):
            call.exit_code = exit_code
        call.__post_init__()
        resolved_tools.add(call.call_id)

    for rec in records:
        etype = rec.get("type", "")
        data = rec.get("data", {})
        if not isinstance(data, dict):
            add_diagnostic("malformed_payload", record_line(rec))
            data = {}
        ts = rec.get("timestamp", "")

        if etype == "user.message":
            # Start a new turn
            if current_turn is not None:
                turns.append(current_turn)
            turn_idx += 1
            current_turn = Turn(index=turn_idx, timestamp=ts)
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("text", str(p))) if isinstance(p, dict) else str(p)
                    for p in content
                )
            elif not isinstance(content, str):
                content = str(content)
            current_turn.user_input = content

        elif etype == "assistant.message":
            if current_turn is None:
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=ts)
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    str(p.get("text", str(p))) if isinstance(p, dict) else str(p)
                    for p in content
                )
            elif not isinstance(content, str):
                content = str(content)
            current_turn.assistant_output += content

        elif etype == "assistant.turn_start":
            # Context may be embedded here in some versions
            pass

        elif etype == "assistant.turn_end":
            # Mark end-of-turn; turn gets committed at next user.message
            pass

        elif etype == "tool.execution_start":
            if current_turn is None:
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=ts)
            call_id = data.get("toolCallId", "")
            name = data.get("toolName", "")
            args = decode_arguments(data.get("arguments", {}), record_line(rec))
            if not call_id:
                add_diagnostic("missing_call_id", record_line(rec))
                call_id = f"unknown-tool-{len(current_turn.tool_calls) + 1 if current_turn else 1}"
            if call_id in pending_tools:
                add_diagnostic("duplicate_call_id", record_line(rec), call_id=call_id)
                call_id = f"{call_id}#duplicate-{len(current_turn.tool_calls) + 1}"
            tc = ToolCall(
                name=str(name or "unknown"),
                arguments=args,
                call_id=str(call_id),
                source_ref=f"{path.name}#L{record_line(rec)}",
            )
            pending_tools[call_id] = tc
            current_turn.tool_calls.append(tc)
            if call_id in pending_results:
                result_output, result_success, result_exit_code = pending_results.pop(call_id)
                attach_result(tc, result_output, result_success, result_exit_code)
            # Extract context clues from tool args
            if tc.name in ("bash", "shell", "exec_command"):
                cmd = args.get("command", args.get("cmd", ""))
                if cmd:
                    current_turn.context_meta.setdefault("has_shell", True)

        elif etype == "tool.execution_complete":
            call_id = data.get("toolCallId", "")
            success = data.get("success", None)
            result = data.get("result", {})
            output = ""
            if isinstance(result, dict):
                output = result.get("content", result.get("output", ""))
                if isinstance(output, (list, dict)):
                    output = json.dumps(output, ensure_ascii=False)
            elif isinstance(result, str):
                output = result
            else:
                output = "" if output is None else str(output)

            exit_code = data.get("exitCode", data.get("exit_code"))
            if call_id in pending_tools:
                attach_result(pending_tools[call_id], str(output or ""), success, exit_code)
            else:
                pending_results[str(call_id)] = (str(output or ""), success, exit_code)

            if current_turn is not None:
                current_turn.raw_tool_output_chars += len(str(output or ""))

                # Extract context clues from output
                _extract_copilot_context(output, current_turn)

        elif etype == "session.truncation":
            session.context_compacted_count += 1
            if current_turn is not None:
                current_turn.events.append({"type": "context_compacted"})

        elif etype not in {
            "session.start",
            "session.model_change",
            "assistant.turn_start",
            "assistant.turn_end",
        }:
            if current_turn is None:
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=ts)
            current_turn.events.append({"type": etype or "unknown", "status": "unknown", "raw": data})
            add_diagnostic("unknown_record", record_line(rec), record_type=etype)

    # Commit last turn
    if current_turn is not None:
        turns.append(current_turn)

    session.turns = turns
    for call_id, call in pending_tools.items():
        if call_id not in resolved_tools:
            session.diagnostics.append({
                "kind": "missing_call_result",
                "call_id": call_id,
                "status": "unknown",
            })
    for call_id in set(pending_results).union(orphan_results):
        if call_id not in pending_tools:
            session.diagnostics.append({
                "kind": "orphan_call_result",
                "call_id": call_id,
                "status": "unknown",
            })
    return session


def _extract_copilot_context(output: str, turn: Turn) -> None:
    """Extract context clues from tool outputs."""
    if not output:
        return
    lower = output.lower()
    if any(kw in lower for kw in ("pwd", "current working directory", "/home/")):
        turn.context_meta["cwd_present"] = True
    if any(kw in lower for kw in ("exit code", "exited with exit code", "exit_code")):
        turn.context_meta["exit_code_present"] = True
    if "permission" in lower or "denied" in lower:
        turn.context_meta["permission_present"] = True
    if "git" in lower and any(kw in lower for kw in ("branch", "status", "commit", "repository")):
        turn.context_meta["git_present"] = True
