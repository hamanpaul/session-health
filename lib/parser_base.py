"""Shared data structures for session parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any, Dict, List, Optional


MAX_SESSION_INPUT_BYTES = 8_000_000
MAX_SESSION_RECORDS = 50_000
MAX_SESSION_RECORD_CHARS = 1_000_000


def read_jsonl_records(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a bounded JSONL input while retaining line/record provenance.

    The adapters deliberately stop before constructing an unbounded in-memory
    record list.  A file that is larger than the input contract is rejected so
    a caller can report it as a failed selected input rather than silently
    scoring a prefix as if it were complete.
    """

    source = Path(path)
    size = source.stat().st_size
    if size > MAX_SESSION_INPUT_BYTES:
        raise ValueError(
            f"session input exceeds max_bytes={MAX_SESSION_INPUT_BYTES}"
        )
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    with open(source, "r", encoding="utf-8", errors="replace") as handle:
        for line_number, raw_line in enumerate(handle, 1):
            line = raw_line.strip()
            if not line:
                continue
            if len(line) > MAX_SESSION_RECORD_CHARS:
                diagnostics.append({
                    "kind": "oversize_record",
                    "line": line_number,
                    "status": "failed",
                })
                continue
            if len(records) >= MAX_SESSION_RECORDS:
                diagnostics.append({
                    "kind": "record_limit_exceeded",
                    "line": line_number,
                    "status": "failed",
                })
                break
            try:
                record = json.loads(line, parse_constant=reject_constant)
            except (json.JSONDecodeError, ValueError):
                diagnostics.append({
                    "kind": "malformed_record",
                    "line": line_number,
                    "status": "unknown",
                })
                continue
            if isinstance(record, dict):
                record["__source_line__"] = line_number
                record["__record_sequence__"] = len(records) + 1
                records.append(record)
            else:
                diagnostics.append({
                    "kind": "non_object_record",
                    "line": line_number,
                    "status": "unknown",
                })
    return records, diagnostics


@dataclass
class ToolCall:
    """A single tool/function invocation within a turn."""
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)
    call_id: str = ""
    output: str = ""
    success: Optional[bool] = None
    exit_code: Optional[int] = None
    status: str = "unknown"
    source_ref: str = ""
    diagnostics: List[str] = field(default_factory=list)
    raw_call_id: str = ""
    result_source_ref: str = ""
    timestamp: str = ""
    result_timestamp: str = ""
    result_sequence: int = 0
    sequence: int = 0
    result_metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Keep the three-valued outcome explicit and backwards compatible."""

        if not self.raw_call_id:
            self.raw_call_id = self.call_id
        if self.success is True:
            self.status = "success"
        elif self.success is False:
            self.status = "failed"
        elif self.exit_code is not None:
            self.status = "success" if self.exit_code == 0 else "failed"
        elif self.status not in {"success", "failed", "unknown"}:
            self.status = "unknown"


@dataclass
class Turn:
    """One interaction round: user input → assistant response (+ tool calls).

    Each turn represents the unit of evaluation for per-turn scoring.
    """
    index: int
    user_input: str = ""
    assistant_output: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    events: List[Dict[str, Any]] = field(default_factory=list)
    context_meta: Dict[str, Any] = field(default_factory=dict)
    timestamp: str = ""

    # Cached raw text sizes for SNR calculation
    raw_tool_output_chars: int = 0
    total_context_chars: int = 0
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def has_tools(self) -> bool:
        return len(self.tool_calls) > 0

    @property
    def shell_commands(self) -> List[str]:
        """Extract shell command strings from tool calls."""
        cmds: List[str] = []
        for tc in self.tool_calls:
            if tc.name in ("exec_command", "shell", "shell_command", "bash"):
                cmd = tc.arguments.get("command", tc.arguments.get("cmd", ""))
                if cmd:
                    cmds.append(str(cmd))
        return cmds


@dataclass
class Session:
    """Parsed session containing metadata and ordered turns."""
    id: str
    source: str  # "codex" | "copilot"
    model: str = ""
    cwd: str = ""
    cli_version: str = ""
    turns: List[Turn] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    timestamp_start: str = ""
    timestamp_end: str = ""

    # Session-level event counters
    context_compacted_count: int = 0
    task_started_count: int = 0
    task_complete_count: int = 0
    turn_aborted_count: int = 0
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    parser_version: str = "legacy-1"
    source_ref: str = ""
    source_capabilities: Dict[str, Any] = field(default_factory=dict)
    # Adapter-owned ordered records.  Bundle construction uses this when it is
    # available so a later result cannot be moved ahead of an earlier claim.
    event_log: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def duration_label(self) -> str:
        if not self.timestamp_start or not self.timestamp_end:
            return "unknown"
        return f"{self.timestamp_start} → {self.timestamp_end}"
