"""LLM Reaction Metrics.

Measures the quality of LLM responses as a proxy for prompt quality:
- Parse error rate: tool calls that fail or produce errors
- Loop rate: identical commands repeated consecutively
- Abort rate: turns that were aborted mid-way

These are *reverse* indicators: high values = bad prompt quality.

Score: 0–100 where 100 = no reaction problems.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ..parser_base import Session, Turn


@dataclass
class ReactionResult:
    """Result of reaction analysis for a single turn."""
    total_commands: int = 0
    repeated_commands: int = 0
    failed_tools: int = 0
    total_tools: int = 0
    is_aborted: bool = False
    loop_rate: float = 0.0    # 0–1
    error_rate: float = 0.0   # 0–1
    score: float = 100.0      # 0–100


def analyze_reaction(turn: Turn, prev_turn: Turn | None = None) -> ReactionResult:
    """Analyze LLM reaction quality for a turn.

    Args:
        turn: Current turn to analyze
        prev_turn: Previous turn (for cross-turn loop detection)
    """
    result = ReactionResult()

    # Count tool calls and failures
    result.total_tools = len(turn.tool_calls)
    for tc in turn.tool_calls:
        if tc.success is False:
            result.failed_tools += 1
        elif tc.output and _looks_like_error(tc.output):
            result.failed_tools += 1

    # Error rate
    if result.total_tools > 0:
        result.error_rate = result.failed_tools / result.total_tools

    # Shell commands for loop detection
    cmds = turn.shell_commands
    result.total_commands = len(cmds)

    # Intra-turn loop: identical consecutive commands
    for i in range(1, len(cmds)):
        if cmds[i].strip() == cmds[i - 1].strip():
            result.repeated_commands += 1

    # Cross-turn loop: same commands as previous turn
    if prev_turn is not None:
        prev_cmds = set(prev_turn.shell_commands)
        if prev_cmds and cmds:
            curr_set = set(cmds)
            overlap = curr_set & prev_cmds
            if len(overlap) > 0 and len(overlap) >= len(curr_set) * 0.8:
                result.repeated_commands += len(overlap)
                result.total_commands += len(overlap)

    # Loop rate
    if result.total_commands > 0:
        result.loop_rate = result.repeated_commands / result.total_commands

    # Abort detection
    result.is_aborted = any(
        e.get("type") in ("turn_aborted",) for e in turn.events
    )

    # Scoring: 100 = no problems, penalize for errors/loops/aborts
    error_penalty = result.error_rate * 40
    loop_penalty = result.loop_rate * 35
    abort_penalty = 25 if result.is_aborted else 0

    result.score = max(0, 100 - error_penalty - loop_penalty - abort_penalty)

    return result


def analyze_reaction_session(session: Session) -> List[ReactionResult]:
    """Analyze reaction metrics for all turns in a session."""
    results: List[ReactionResult] = []
    prev: Turn | None = None
    for turn in session.turns:
        results.append(analyze_reaction(turn, prev))
        prev = turn
    return results


def _looks_like_error(output: str) -> bool:
    """Heuristic: does the output look like an error?"""
    if not output:
        return False
    lower = output.lower()
    error_indicators = [
        "error:", "traceback", "exception", "fatal:",
        "command not found", "no such file", "permission denied",
        "segmentation fault", "killed", "oom",
    ]
    # Must have error indicator AND be relatively short (not just a log with 'error' in it)
    has_indicator = any(ind in lower for ind in error_indicators)
    if not has_indicator:
        return False
    # If output is very long, it's probably a log, not a pure error
    return len(output) < 2000
