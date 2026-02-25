"""Reasoning Depth metric (推理深度).

Measures the quality and density of agent reasoning within turns.
Good prompts lead to thoughtful reasoning before action, not blind execution.

Sub-metrics:
  1. Reasoning density: ratio of reasoning events to total events
  2. Think-before-act: whether reasoning precedes tool calls
  3. Reasoning length: adequate reasoning vs. trivially short

Score: 0–100 where 100 = excellent reasoning depth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List

from ..parser_base import Session, Turn


@dataclass
class DepthResult:
    """Result of reasoning depth analysis for a single turn."""
    has_reasoning: bool = False
    reasoning_chars: int = 0
    assistant_chars: int = 0
    tool_count: int = 0
    reasoning_before_action: bool = True
    score: float = 50.0  # 0–100, default neutral


def analyze_depth(turn: Turn) -> DepthResult:
    """Analyze reasoning depth for a single turn."""
    result = DepthResult()
    result.assistant_chars = len(turn.assistant_output)
    result.tool_count = len(turn.tool_calls)

    # Check for reasoning events
    for ev in turn.events:
        if ev.get("type") in ("agent_reasoning", "reasoning"):
            result.has_reasoning = True
            info = ev.get("info", "")
            if isinstance(info, str):
                result.reasoning_chars += len(info)

    # Also count assistant output as reasoning proxy
    # (in Copilot CLI, reasoning is embedded in assistant messages)
    if not result.has_reasoning and result.assistant_chars > 0:
        result.has_reasoning = True
        result.reasoning_chars = result.assistant_chars

    # If no tools and no reasoning, it's a simple Q&A turn — neutral score
    if result.tool_count == 0 and not result.has_reasoning:
        result.score = 50.0
        return result

    # Scoring components:

    # 1. Has reasoning at all? (30 pts)
    reasoning_present_score = 30.0 if result.has_reasoning else 0.0

    # 2. Reasoning density relative to action (40 pts)
    if result.tool_count > 0:
        # Good: at least 50 chars of reasoning per tool call
        chars_per_tool = result.reasoning_chars / result.tool_count
        density_score = min(40.0, (chars_per_tool / 100) * 40)
    elif result.has_reasoning:
        density_score = 40.0  # Pure reasoning turn, full marks
    else:
        density_score = 0.0

    # 3. Adequate reasoning length (30 pts)
    if result.reasoning_chars > 200:
        length_score = 30.0
    elif result.reasoning_chars > 50:
        length_score = 20.0
    elif result.reasoning_chars > 0:
        length_score = 10.0
    else:
        length_score = 0.0

    result.score = min(100.0, reasoning_present_score + density_score + length_score)
    return result


def analyze_depth_session(session: Session) -> List[DepthResult]:
    """Analyze reasoning depth for all turns in a session."""
    return [analyze_depth(t) for t in session.turns]
