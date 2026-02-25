"""Tool Efficiency metric (TOOL).

Measures how effectively the agent uses tools:
- Success rate: proportion of tool calls that succeed
- Redundancy: consecutive identical tool calls (same name + same args)
- Diversity: variety of tools used vs total calls
- Output utilization: whether tool output content appears in subsequent responses

Score: 0–100
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..parser_base import Turn, Session


@dataclass
class ToolEfficiencyResult:
    """Result of tool efficiency analysis for a turn."""
    score: float = 100.0
    total_calls: int = 0
    successful_calls: int = 0
    failed_calls: int = 0
    redundant_calls: int = 0
    success_rate: float = 1.0
    details: List[str] = field(default_factory=list)


def analyze_tool_efficiency(turn: Turn, prev_turn: Optional[Turn] = None) -> ToolEfficiencyResult:
    """Analyze tool usage efficiency for a single turn.

    Components:
      - Success rate (40 pts): ratio of successful tool calls
      - Non-redundancy (30 pts): penalty for consecutive identical calls
      - Output utilization (30 pts): tool output referenced in assistant response
    """
    if not turn.tool_calls:
        # No tools used — neutral score
        return ToolEfficiencyResult(score=100.0, details=["No tool calls in this turn"])

    total = len(turn.tool_calls)
    successful = 0
    failed = 0
    redundant = 0

    # Track success/failure
    for tc in turn.tool_calls:
        if tc.success is True:
            successful += 1
        elif tc.success is False:
            failed += 1
        elif tc.exit_code is not None:
            if tc.exit_code == 0:
                successful += 1
            else:
                failed += 1
        else:
            # Unknown — assume success (no evidence of failure)
            successful += 1

    success_rate = successful / total if total > 0 else 1.0

    # Detect redundant calls: consecutive same name + same arguments
    prev_calls = prev_turn.tool_calls if prev_turn else []
    all_calls = prev_calls + turn.tool_calls
    for i in range(len(prev_calls), len(all_calls)):
        if i > 0:
            curr = all_calls[i]
            prev = all_calls[i - 1]
            if curr.name == prev.name and curr.arguments == prev.arguments:
                redundant += 1

    redundancy_rate = redundant / total if total > 0 else 0.0

    # Output utilization: check if tool output snippets appear in assistant response
    utilized = 0
    if turn.assistant_output and total > 0:
        assistant_lower = turn.assistant_output.lower()
        for tc in turn.tool_calls:
            if tc.output:
                # Check if meaningful portion of output is referenced
                # Use first 100 chars of output as a check
                snippet = tc.output.strip()[:100].lower()
                if len(snippet) > 20 and snippet[:40] in assistant_lower:
                    utilized += 1
                elif len(snippet) <= 20 and snippet in assistant_lower:
                    utilized += 1
        utilization_rate = utilized / total
    else:
        # If no assistant output to check against, give partial credit
        utilization_rate = 0.5

    # Scoring
    success_score = success_rate * 40  # 0-40 pts
    redundancy_score = (1 - redundancy_rate) * 30  # 0-30 pts
    utilization_score = utilization_rate * 30  # 0-30 pts

    score = success_score + redundancy_score + utilization_score
    score = max(0, min(100, score))

    details = []
    if failed > 0:
        details.append(f"{failed}/{total} tool calls failed")
    if redundant > 0:
        details.append(f"{redundant} redundant calls detected")

    return ToolEfficiencyResult(
        score=score,
        total_calls=total,
        successful_calls=successful,
        failed_calls=failed,
        redundant_calls=redundant,
        success_rate=success_rate,
        details=details,
    )


def analyze_tool_efficiency_session(session: Session) -> List[ToolEfficiencyResult]:
    """Analyze tool efficiency for all turns in a session."""
    results: List[ToolEfficiencyResult] = []
    for i, turn in enumerate(session.turns):
        prev = session.turns[i - 1] if i > 0 else None
        results.append(analyze_tool_efficiency(turn, prev))
    return results
