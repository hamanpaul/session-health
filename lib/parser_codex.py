"""Codex CLI JSONL session parser.

Parses session logs from ~/.codex/sessions/{YYYY}/{MM}/{session}.jsonl
Record types: session_meta, response_item, event_msg, function_call,
              function_call_output, reasoning, state, turn_context, compacted
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

from .parser_base import Session, Turn, ToolCall


def parse_codex_session(path: str | Path) -> Session:
    """Parse a Codex CLI JSONL session file into a Session object."""
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

    session = Session(id="", source="codex")

    # --- Extract session metadata ---
    for rec in records:
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})

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

    for rec in records:
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})
        ts = rec.get("timestamp", "")

        if rtype == "response_item":
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

        elif rtype == "function_call":
            call_id = payload.get("call_id", payload.get("id", ""))
            name = payload.get("name", "")
            args = payload.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {"raw": args}
            tc = ToolCall(name=name, arguments=args, call_id=call_id)
            pending_calls[call_id] = tc
            if current_turn is not None:
                current_turn.tool_calls.append(tc)

        elif rtype == "function_call_output":
            call_id = payload.get("call_id", payload.get("id", ""))
            output = payload.get("output", "")
            if isinstance(output, dict):
                output = json.dumps(output)
            if call_id in pending_calls:
                pending_calls[call_id].output = output
            if current_turn is not None:
                current_turn.raw_tool_output_chars += len(output)

        elif rtype == "event_msg":
            event_type = payload.get("type", "")
            if current_turn is not None:
                current_turn.events.append(payload)
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
            if current_turn is not None:
                ctx_text = _extract_text(payload.get("content", []))
                current_turn.total_context_chars += len(ctx_text)

    # Don't forget the last turn
    if current_turn is not None:
        turns.append(current_turn)

    session.turns = turns
    return session


def _extract_text(content_parts: list) -> str:
    """Extract text from content parts array."""
    texts = []
    for part in content_parts:
        if isinstance(part, str):
            texts.append(part)
        elif isinstance(part, dict):
            texts.append(part.get("text", part.get("input_text", "")))
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
