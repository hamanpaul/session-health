"""Copilot CLI JSONL session parser.

Parses session logs from ~/.copilot/session-state/{uuid}.jsonl
Event types: session.start, session.model_change, user.message,
             assistant.message, assistant.turn_start, assistant.turn_end,
             tool.execution_start, tool.execution_complete, session.truncation
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .parser_base import Session, Turn, ToolCall


def parse_copilot_session(path: str | Path) -> Session:
    """Parse a Copilot CLI JSONL session file into a Session object."""
    path = Path(path)
    records: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    session = Session(id="", source="copilot")

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

    for rec in records:
        etype = rec.get("type", "")
        data = rec.get("data", {})
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
                    p.get("text", str(p)) if isinstance(p, dict) else str(p)
                    for p in content
                )
            current_turn.user_input = content

        elif etype == "assistant.message":
            if current_turn is None:
                turn_idx += 1
                current_turn = Turn(index=turn_idx, timestamp=ts)
            content = data.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", str(p)) if isinstance(p, dict) else str(p)
                    for p in content
                )
            current_turn.assistant_output += content

        elif etype == "assistant.turn_start":
            # Context may be embedded here in some versions
            pass

        elif etype == "assistant.turn_end":
            # Mark end-of-turn; turn gets committed at next user.message
            pass

        elif etype == "tool.execution_start":
            call_id = data.get("toolCallId", "")
            name = data.get("toolName", "")
            args = data.get("arguments", {})
            tc = ToolCall(name=name, arguments=args, call_id=call_id)
            pending_tools[call_id] = tc
            if current_turn is not None:
                current_turn.tool_calls.append(tc)
                # Extract context clues from tool args
                if name in ("bash", "shell", "exec_command"):
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
                    output = json.dumps(output)
            elif isinstance(result, str):
                output = result

            if call_id in pending_tools:
                pending_tools[call_id].output = output
                pending_tools[call_id].success = success

            if current_turn is not None:
                current_turn.raw_tool_output_chars += len(output)

                # Extract context clues from output
                _extract_copilot_context(output, current_turn)

        elif etype == "session.truncation":
            session.context_compacted_count += 1
            if current_turn is not None:
                current_turn.events.append({"type": "context_compacted"})

    # Commit last turn
    if current_turn is not None:
        turns.append(current_turn)

    session.turns = turns
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
