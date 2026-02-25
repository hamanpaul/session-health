"""Context Memory Management metric.

Measures how well the session retains the user's original goal
and avoids context bloat from unsummarized raw logs.

Sub-metrics:
  1. Goal Retention: do keywords from the first user message persist?
  2. History Redundancy: ratio of raw tool output to total context size
  3. Compaction Events: frequency of context truncation/compaction

Score: 0–100 where 100 = perfect memory management.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Set

from ..parser_base import Session, Turn

# Simple tokenizer: split on whitespace and punctuation
_TOKEN_RE = re.compile(r"[a-zA-Z\u4e00-\u9fff\u3400-\u4dbf]{2,}")

# Common stop words to exclude from goal keywords
_STOP_WORDS: Set[str] = {
    "the", "is", "at", "in", "on", "to", "for", "of", "and", "or", "not",
    "this", "that", "with", "from", "but", "are", "was", "were", "been",
    "have", "has", "had", "will", "can", "do", "does", "did", "should",
    "would", "could", "may", "might", "please", "help", "want", "need",
    "use", "using", "used", "make", "like", "just", "also", "get",
    "的", "是", "在", "了", "我", "你", "他", "她", "它", "們", "這", "那",
    "和", "與", "或", "不", "也", "都", "要", "會", "可以", "請", "幫",
}


@dataclass
class ContextResult:
    """Result of context memory analysis for a turn."""
    goal_keywords: Set[str] = field(default_factory=set)
    keywords_present: Set[str] = field(default_factory=set)
    goal_retention_rate: float = 1.0  # 0–1
    raw_output_ratio: float = 0.0    # 0–1, ratio of raw tool output
    has_compaction: bool = False
    score: float = 100.0  # 0–100


def extract_goal_keywords(text: str, max_keywords: int = 15) -> Set[str]:
    """Extract meaningful keywords from the initial user message."""
    tokens = _TOKEN_RE.findall(text.lower())
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]

    # Use frequency for ranking (simple TF)
    freq: Dict[str, int] = {}
    for kw in keywords:
        freq[kw] = freq.get(kw, 0) + 1

    ranked = sorted(freq.keys(), key=lambda k: freq[k], reverse=True)
    return set(ranked[:max_keywords])


def analyze_context(
    turn: Turn,
    goal_keywords: Set[str],
    turn_position: int,
    total_turns: int,
) -> ContextResult:
    """Analyze context memory for a single turn.

    Args:
        turn: The turn to analyze
        goal_keywords: Keywords from the original user goal
        turn_position: 0-based index of this turn
        total_turns: Total turns in session
    """
    result = ContextResult(goal_keywords=goal_keywords)

    # 1. Goal retention: check if goal keywords appear in this turn's context
    if goal_keywords:
        turn_text = (
            turn.user_input + " " + turn.assistant_output
        ).lower()
        for tc in turn.tool_calls:
            turn_text += " " + tc.output.lower()

        result.keywords_present = {
            kw for kw in goal_keywords if kw in turn_text
        }
        result.goal_retention_rate = (
            len(result.keywords_present) / len(goal_keywords)
            if goal_keywords
            else 1.0
        )

    # 2. Raw output ratio: how much of this turn is raw tool output vs total?
    total_chars = (
        len(turn.user_input)
        + len(turn.assistant_output)
        + turn.raw_tool_output_chars
        + turn.total_context_chars
    )
    if total_chars > 0:
        result.raw_output_ratio = turn.raw_tool_output_chars / total_chars

    # 3. Compaction events
    result.has_compaction = any(
        e.get("type") == "context_compacted" for e in turn.events
    )

    # Scoring:
    # - Goal retention contributes 60% (weighted more for later turns)
    # - Raw output ratio penalty: 40%
    position_weight = min(1.0, (turn_position + 1) / max(total_turns, 1))
    retention_score = result.goal_retention_rate * 100
    # Later turns losing goal = worse
    retention_weighted = retention_score * (0.4 + 0.6 * position_weight)

    # Penalize high raw output ratio (> 0.7 is bad)
    redundancy_penalty = max(0, (result.raw_output_ratio - 0.3)) * 100 / 0.7
    redundancy_penalty = min(redundancy_penalty, 40)

    # Compaction is a warning sign but not always bad
    compaction_penalty = 5 if result.has_compaction else 0

    result.score = max(0, min(100, retention_weighted - redundancy_penalty - compaction_penalty))

    return result


def analyze_context_session(session: Session) -> List[ContextResult]:
    """Analyze context memory for all turns in a session."""
    if not session.turns:
        return []

    # Extract goal from first user message
    first_user_msg = ""
    for turn in session.turns:
        if turn.user_input:
            first_user_msg = turn.user_input
            break

    goal_keywords = extract_goal_keywords(first_user_msg)
    total = len(session.turns)

    return [
        analyze_context(t, goal_keywords, i, total)
        for i, t in enumerate(session.turns)
    ]
