"""Environment State Integrity metric.

Measures how well each turn's context includes critical state information
that the LLM needs for correct decision-making.

Checklist items:
  - cwd (current working directory)
  - exit_code (previous command exit code)
  - permission (sandbox/permission context)
  - git (repository/branch status)

Score: 0–100 where 100 = all checklist items present.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List

from ..parser_base import Turn

# Default checklist weights (sum to 1.0)
DEFAULT_CHECKLIST: Dict[str, float] = {
    "cwd_present": 0.35,       # Most critical: where are we?
    "exit_code_present": 0.30,  # Did the last command succeed?
    "permission_present": 0.15, # What can we do?
    "git_present": 0.20,       # Repository context
}


@dataclass
class StateResult:
    """Result of state integrity analysis for a single turn."""
    checklist: Dict[str, bool] = field(default_factory=dict)
    weights: Dict[str, float] = field(default_factory=dict)
    score: float = 0.0  # 0–100

    @property
    def present_count(self) -> int:
        return sum(1 for v in self.checklist.values() if v)

    @property
    def total_count(self) -> int:
        return len(self.checklist)

    @property
    def coverage_pct(self) -> float:
        if not self.checklist:
            return 0.0
        return self.present_count / self.total_count * 100


def analyze_state(turn: Turn, checklist: Dict[str, float] | None = None) -> StateResult:
    """Analyze state integrity for a turn.

    Checks whether the turn's context_meta contains each checklist item.
    For turns without tool calls, state requirements are relaxed.
    """
    weights = checklist or DEFAULT_CHECKLIST
    result = StateResult(weights=weights)

    # If turn has no tool calls, state context is less critical
    if not turn.has_tools:
        result.checklist = {k: True for k in weights}
        result.score = 100.0
        return result

    # Check each item against turn.context_meta
    for key in weights:
        result.checklist[key] = turn.context_meta.get(key, False)

    # Weighted score
    total_weight = sum(weights.values())
    if total_weight == 0:
        result.score = 100.0
        return result

    weighted_sum = sum(
        weights[k] for k, present in result.checklist.items() if present
    )
    result.score = (weighted_sum / total_weight) * 100

    return result


def analyze_state_session(turns: List[Turn]) -> List[StateResult]:
    """Analyze state integrity for all turns in a session."""
    return [analyze_state(t) for t in turns]
