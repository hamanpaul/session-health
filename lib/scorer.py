"""Composite scorer and formula engine.

Combines the 4 metric dimensions into per-turn and session-level scores.

Formula:
  Turn Score = StateIntegrity - NoisePenalty - ReactionPenalty
  Session Score = mean(Turn Scores)

Each dimension is also reported independently (0–100 scale) for the radar chart.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Dict, List

from .parser_base import Session
from .metrics.snr import SNRResult, analyze_snr
from .metrics.state import StateResult, analyze_state
from .metrics.context import ContextResult, analyze_context_session
from .metrics.reaction import ReactionResult, analyze_reaction_session


@dataclass
class TurnScore:
    """Composite score for a single turn."""
    index: int
    snr: float = 100.0
    state: float = 100.0
    context: float = 100.0
    reaction: float = 100.0
    composite: float = 100.0

    # Raw metric results for drill-down
    snr_result: SNRResult | None = None
    state_result: StateResult | None = None
    context_result: ContextResult | None = None
    reaction_result: ReactionResult | None = None


@dataclass
class SessionScore:
    """Aggregate score for an entire session."""
    session_id: str
    source: str
    model: str
    turn_count: int

    # Per-dimension averages (0–100)
    snr: float = 0.0
    state: float = 0.0
    context: float = 0.0
    reaction: float = 0.0
    composite: float = 0.0

    # Statistics
    composite_min: float = 0.0
    composite_max: float = 0.0
    composite_stddev: float = 0.0

    # Per-turn breakdown
    turn_scores: List[TurnScore] = field(default_factory=list)

    # Session-level event counts
    compaction_count: int = 0
    abort_count: int = 0

    @property
    def radar_axes(self) -> Dict[str, float]:
        """Return the 4 radar chart axes."""
        return {
            "SNR": self.snr,
            "STATE": self.state,
            "CTX": self.context,
            "REACT": self.reaction,
        }

    @property
    def grade(self) -> str:
        """Letter grade based on composite score."""
        s = self.composite
        if s >= 90:
            return "A"
        elif s >= 80:
            return "B"
        elif s >= 70:
            return "C"
        elif s >= 60:
            return "D"
        else:
            return "F"

    @property
    def grade_label(self) -> str:
        labels = {
            "A": "excellent",
            "B": "good",
            "C": "fair",
            "D": "needs improvement",
            "F": "poor",
        }
        return labels.get(self.grade, "unknown")


def score_session(session: Session) -> SessionScore:
    """Score an entire session across all 4 dimensions."""
    if not session.turns:
        return SessionScore(
            session_id=session.id,
            source=session.source,
            model=session.model,
            turn_count=0,
        )

    # Run per-turn metrics
    context_results = analyze_context_session(session)
    reaction_results = analyze_reaction_session(session)

    turn_scores: List[TurnScore] = []

    for i, turn in enumerate(session.turns):
        snr_r = analyze_snr(turn)
        state_r = analyze_state(turn)
        ctx_r = context_results[i] if i < len(context_results) else ContextResult()
        react_r = reaction_results[i] if i < len(reaction_results) else ReactionResult()

        # Per-turn composite using the formula:
        # composite = state - noise_penalty - reaction_penalty
        noise_penalty = max(0, (100 - snr_r.score)) * 0.5  # scale to 0–50
        reaction_penalty = max(0, (100 - react_r.score)) * 0.5  # scale to 0–50

        composite = state_r.score - noise_penalty - reaction_penalty
        composite = max(0, min(100, composite))

        ts = TurnScore(
            index=turn.index,
            snr=snr_r.score,
            state=state_r.score,
            context=ctx_r.score,
            reaction=react_r.score,
            composite=composite,
            snr_result=snr_r,
            state_result=state_r,
            context_result=ctx_r,
            reaction_result=react_r,
        )
        turn_scores.append(ts)

    # Aggregate
    composites = [ts.composite for ts in turn_scores]
    snrs = [ts.snr for ts in turn_scores]
    states = [ts.state for ts in turn_scores]
    contexts = [ts.context for ts in turn_scores]
    reactions = [ts.reaction for ts in turn_scores]

    result = SessionScore(
        session_id=session.id,
        source=session.source,
        model=session.model,
        turn_count=len(session.turns),
        snr=statistics.mean(snrs),
        state=statistics.mean(states),
        context=statistics.mean(contexts),
        reaction=statistics.mean(reactions),
        composite=statistics.mean(composites),
        composite_min=min(composites),
        composite_max=max(composites),
        composite_stddev=statistics.stdev(composites) if len(composites) > 1 else 0.0,
        turn_scores=turn_scores,
        compaction_count=session.context_compacted_count,
        abort_count=session.turn_aborted_count,
    )

    return result
