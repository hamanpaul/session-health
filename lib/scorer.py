"""Composite scorer and formula engine.

Combines 6 metric dimensions into per-turn and session-level scores.

Dimensions:
  SNR    (信噪比)    — 終端輸出中無效雜訊的佔比
  STATE  (狀態完整度) — 環境關鍵資訊（cwd、exit code、權限）的覆蓋率
  CTX    (記憶留存)   — 多輪對話中原始目標是否被遺忘、歷史 log 冗餘度
  REACT  (反應指標)   — LLM 是否出現死迴圈、解析錯誤等異常反應
  DEPTH  (推理深度)   — Agent 推理區塊的密度與品質，是否先思考再行動
  CONV   (收斂力)    — 任務是否順利完成、是否中途 abort/重啟

Formula:
  Turn Score = State - NoisePenalty - ReactionPenalty + DepthBonus
  Session Score = mean(Turn Scores), adjusted by Convergence
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
from .metrics.depth import DepthResult, analyze_depth
from .metrics.convergence import ConvergenceResult, analyze_convergence
from .metrics.tool_efficiency import ToolEfficiencyResult, analyze_tool_efficiency_session


@dataclass
class TurnScore:
    """Composite score for a single turn."""
    index: int
    snr: float = 100.0
    state: float = 100.0
    context: float = 100.0
    reaction: float = 100.0
    depth: float = 50.0
    tool_efficiency: float = 100.0
    composite: float = 100.0

    # Raw metric results for drill-down
    snr_result: SNRResult | None = None
    state_result: StateResult | None = None
    context_result: ContextResult | None = None
    reaction_result: ReactionResult | None = None
    depth_result: DepthResult | None = None
    tool_result: ToolEfficiencyResult | None = None


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
    depth: float = 0.0
    convergence: float = 0.0
    tool_efficiency: float = 0.0
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
        """Return the 7 radar chart axes."""
        return {
            "SNR": self.snr,
            "STATE": self.state,
            "REACT": self.reaction,
            "CONV": self.convergence,
            "CTX": self.context,
            "DEPTH": self.depth,
            "TOOL": self.tool_efficiency,
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
    """Score an entire session across all 6 dimensions."""
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
    tool_results = analyze_tool_efficiency_session(session)

    # Session-level metrics
    conv_result = analyze_convergence(session)

    turn_scores: List[TurnScore] = []

    for i, turn in enumerate(session.turns):
        snr_r = analyze_snr(turn)
        state_r = analyze_state(turn)
        ctx_r = context_results[i] if i < len(context_results) else ContextResult()
        react_r = reaction_results[i] if i < len(reaction_results) else ReactionResult()
        depth_r = analyze_depth(turn)
        tool_r = tool_results[i] if i < len(tool_results) else ToolEfficiencyResult()

        # Per-turn composite:
        # base = state, penalties for noise/reaction, bonus for depth
        noise_penalty = max(0, (100 - snr_r.score)) * 0.4
        reaction_penalty = max(0, (100 - react_r.score)) * 0.4
        depth_bonus = (depth_r.score - 50) * 0.2  # ±10 points

        composite = state_r.score - noise_penalty - reaction_penalty + depth_bonus
        composite = max(0, min(100, composite))

        ts = TurnScore(
            index=turn.index,
            snr=snr_r.score,
            state=state_r.score,
            context=ctx_r.score,
            reaction=react_r.score,
            depth=depth_r.score,
            tool_efficiency=tool_r.score,
            composite=composite,
            snr_result=snr_r,
            state_result=state_r,
            context_result=ctx_r,
            reaction_result=react_r,
            depth_result=depth_r,
            tool_result=tool_r,
        )
        turn_scores.append(ts)

    # Aggregate
    composites = [ts.composite for ts in turn_scores]
    snrs = [ts.snr for ts in turn_scores]
    states = [ts.state for ts in turn_scores]
    contexts = [ts.context for ts in turn_scores]
    reactions = [ts.reaction for ts in turn_scores]
    depths = [ts.depth for ts in turn_scores]
    tools = [ts.tool_efficiency for ts in turn_scores]

    # Session composite = mean of turn composites, adjusted by convergence
    raw_composite = statistics.mean(composites)
    conv_adjustment = (conv_result.score - 50) * 0.1  # ±5 points
    final_composite = max(0, min(100, raw_composite + conv_adjustment))

    result = SessionScore(
        session_id=session.id,
        source=session.source,
        model=session.model,
        turn_count=len(session.turns),
        snr=statistics.mean(snrs),
        state=statistics.mean(states),
        context=statistics.mean(contexts),
        reaction=statistics.mean(reactions),
        depth=statistics.mean(depths),
        convergence=conv_result.score,
        tool_efficiency=statistics.mean(tools),
        composite=final_composite,
        composite_min=min(composites),
        composite_max=max(composites),
        composite_stddev=statistics.stdev(composites) if len(composites) > 1 else 0.0,
        turn_scores=turn_scores,
        compaction_count=session.context_compacted_count,
        abort_count=session.turn_aborted_count,
    )

    return result
