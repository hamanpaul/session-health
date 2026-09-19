"""Shared data structures for session parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


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

    def __post_init__(self) -> None:
        """Keep the three-valued outcome explicit and backwards compatible."""

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

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def duration_label(self) -> str:
        if not self.timestamp_start or not self.timestamp_end:
            return "unknown"
        return f"{self.timestamp_start} → {self.timestamp_end}"
