"""Convergence metric (收斂力).

Measures how effectively the session converges toward task completion.
Good prompts help the LLM stay focused and complete tasks without
unnecessary restarts, aborts, or context resets.

Sub-metrics:
  1. Task completion rate: started vs completed
  2. Abort rate: proportion of aborted turns
  3. Context reset frequency: compaction events as % of turns
  4. Progressive momentum: are later turns still productive?

Score: 0–100 where 100 = perfect convergence.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..parser_base import Session


@dataclass
class ConvergenceResult:
    """Result of convergence analysis for an entire session."""
    task_started: int = 0
    task_completed: int = 0
    turns_total: int = 0
    turns_aborted: int = 0
    compaction_events: int = 0
    completion_rate: float = 1.0     # 0–1
    abort_rate: float = 0.0          # 0–1
    compaction_rate: float = 0.0     # 0–1
    score: float = 100.0             # 0–100


def analyze_convergence(session: Session) -> ConvergenceResult:
    """Analyze convergence for an entire session.

    This is a session-level metric (not per-turn) because convergence
    is inherently about the trajectory across all turns.
    """
    result = ConvergenceResult()
    result.turns_total = len(session.turns)
    result.task_started = session.task_started_count
    result.task_completed = session.task_complete_count
    result.compaction_events = session.context_compacted_count

    # Count aborted turns
    for turn in session.turns:
        if any(e.get("type") in ("turn_aborted",) for e in turn.events):
            result.turns_aborted += 1

    # Also use session-level counter if per-turn count is lower
    result.turns_aborted = max(result.turns_aborted, session.turn_aborted_count)

    # Completion rate
    if result.task_started > 0:
        result.completion_rate = min(1.0, result.task_completed / result.task_started)
    else:
        # No explicit task lifecycle events — infer from turn patterns
        # If session has turns and no aborts, assume good convergence
        result.completion_rate = 1.0 if result.turns_aborted == 0 else 0.5

    # Abort rate
    if result.turns_total > 0:
        result.abort_rate = result.turns_aborted / result.turns_total

    # Compaction rate
    if result.turns_total > 0:
        result.compaction_rate = result.compaction_events / result.turns_total

    # Scoring:
    # Completion rate: 50 pts
    completion_score = result.completion_rate * 50

    # Abort penalty: up to 30 pts
    abort_penalty = min(30.0, result.abort_rate * 100)

    # Compaction penalty: up to 20 pts (some compaction is okay in long sessions)
    if result.turns_total > 20:
        # Long sessions: compaction is more acceptable
        compaction_penalty = min(10.0, result.compaction_rate * 50)
    else:
        compaction_penalty = min(20.0, result.compaction_rate * 100)

    result.score = max(0.0, min(100.0,
        completion_score + 50 - abort_penalty - compaction_penalty
    ))

    return result
