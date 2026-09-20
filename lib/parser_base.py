"""Shared data structures for session parsing."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import shlex
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class SessionInputLimits:
    """Independent bounds for reading a raw JSONL session input.

    These limits protect the parser's raw-input phase.  They are deliberately
    separate from the much smaller portable-bundle limits, because a source
    session may be large while its exported evidence remains bounded.
    """

    max_bytes: int = 128 * 1024 * 1024
    max_records: int = 50_000
    max_record_chars: int = 1_000_000

    def __post_init__(self) -> None:
        for name, value in (
            ("max_bytes", self.max_bytes),
            ("max_records", self.max_records),
            ("max_record_chars", self.max_record_chars),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")


DEFAULT_SESSION_INPUT_LIMITS = SessionInputLimits()
MAX_SESSION_INPUT_BYTES = DEFAULT_SESSION_INPUT_LIMITS.max_bytes
MAX_SESSION_RECORDS = DEFAULT_SESSION_INPUT_LIMITS.max_records
MAX_SESSION_RECORD_CHARS = DEFAULT_SESSION_INPUT_LIMITS.max_record_chars


def argument_fingerprint(arguments: Any) -> str:
    """Return a portable equality token without transporting arguments."""

    try:
        encoded = json.dumps(
            arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError):
        encoded = repr(arguments)
    return hashlib.sha256(encoded.encode("utf-8", "replace")).hexdigest()


def command_fingerprint(arguments: Any) -> str:
    """Return a one-way identity token for a shell command executable."""

    if not isinstance(arguments, dict):
        return ""
    command = arguments.get("command", arguments.get("cmd", ""))
    if not isinstance(command, str) or not command.strip():
        return ""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return ""
    if not tokens:
        return ""
    return hashlib.sha256(tokens[0].encode("utf-8", "replace")).hexdigest()


def source_ref_line(source_ref: str) -> int | None:
    """Extract a numeric source line from a portable ``path#L<line>`` ref."""

    _, marker, line = str(source_ref).rpartition("#L")
    return int(line) if marker and line.isdigit() else None


def read_jsonl_records(
    path: str | Path,
    limits: SessionInputLimits | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Read a bounded JSONL input while retaining line/record provenance.

    The adapters deliberately stop before constructing an unbounded in-memory
    record list.  When a byte, record-count, or record-size limit is reached,
    the usable records already read are returned with a diagnostic describing
    the discarded input.  Callers can then report partial coverage (or failed
    processing when no usable record remains) instead of scoring a prefix as
    if the complete file had been read.
    """

    source = Path(path)
    limits = limits or DEFAULT_SESSION_INPUT_LIMITS
    records: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    bytes_read = 0

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON constant: {value}")

    with open(source, "r", encoding="utf-8", errors="replace") as handle:
        line_number = 0
        read_chunk_chars = max(1, min(limits.max_record_chars + 1, limits.max_bytes // 4 + 1))
        while True:
            # Bound a single read even when a tool output was serialized as one
            # enormous JSONL line.  The remainder is discarded in bounded
            # chunks and reported as partial coverage.
            raw_line = handle.readline(read_chunk_chars)
            if not raw_line:
                break
            line_number += 1
            line_bytes = len(raw_line.encode("utf-8", "replace"))
            line_chars = len(raw_line.rstrip("\r\n"))
            oversized = line_chars > limits.max_record_chars
            line_parts = [] if oversized else [raw_line]
            complete = raw_line.endswith(("\n", "\r"))
            over_budget = False
            while not complete:
                if bytes_read + line_bytes >= limits.max_bytes:
                    over_budget = True
                    break
                chunk = handle.readline(read_chunk_chars)
                if not chunk:
                    complete = True
                    break
                line_bytes += len(chunk.encode("utf-8", "replace"))
                line_chars += len(chunk.rstrip("\r\n"))
                oversized = line_chars > limits.max_record_chars
                if oversized:
                    line_parts = []
                elif line_parts is not None:
                    line_parts.append(chunk)
                complete = chunk.endswith(("\n", "\r"))
            if over_budget or bytes_read + line_bytes > limits.max_bytes:
                diagnostics.append({
                    "kind": "input_byte_limit_exceeded",
                    "line": line_number,
                    "status": "partial",
                    "max_bytes": limits.max_bytes,
                    "bytes_read": bytes_read,
                })
                break
            bytes_read += line_bytes
            if oversized:
                diagnostics.append({
                    "kind": "oversize_record",
                    "line": line_number,
                    "status": "partial",
                    "max_record_chars": limits.max_record_chars,
                })
                continue
            raw_line = "".join(line_parts)
            line = raw_line.strip()
            if not line:
                continue
            if len(records) >= limits.max_records:
                diagnostics.append({
                    "kind": "record_limit_exceeded",
                    "line": line_number,
                    "status": "partial",
                    "max_records": limits.max_records,
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
    input_limit_kinds = {"input_byte_limit_exceeded", "record_limit_exceeded", "oversize_record"}
    input_limit_status = "partial" if records else "failed"
    for diagnostic in diagnostics:
        if diagnostic.get("kind") in input_limit_kinds:
            diagnostic["status"] = input_limit_status
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
    argument_fingerprint: str = ""
    command_fingerprint: str = ""

    def __post_init__(self) -> None:
        """Keep the three-valued outcome explicit and backwards compatible."""

        if not self.argument_fingerprint:
            self.argument_fingerprint = argument_fingerprint(self.arguments)
        if not self.command_fingerprint:
            self.command_fingerprint = command_fingerprint(self.arguments)
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
    # Complete, non-text SNR sufficient statistics captured before bundle
    # evidence truncation.  Empty means the adapter has not supplied a
    # snapshot and the metric may analyze the available output directly.
    snr_facts: Dict[str, int] = field(default_factory=dict)

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
    # Typed lifecycle identity facts may outlive a bounded event projection.
    # They are intentionally separate from event_log because replay must not
    # invent chronology merely to retain task pairing evidence.
    lifecycle_facts: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    @property
    def duration_label(self) -> str:
        if not self.timestamp_start or not self.timestamp_end:
            return "unknown"
        return f"{self.timestamp_start} → {self.timestamp_end}"
