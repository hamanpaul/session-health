"""Text-based radar chart renderer.

Renders a 4-axis radar chart using Unicode characters for terminal display.
Axes: SNR (top), STATE (right), REACT (bottom), CTX (left).

Supports ANSI colors (can be disabled with --no-color).
"""

from __future__ import annotations

import math
from typing import Dict, List, Tuple

from .scorer import SessionScore

# ANSI color codes
_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bg_blue": "\033[44m",
}


def _c(text: str, color: str, use_color: bool = True) -> str:
    """Apply ANSI color to text."""
    if not use_color:
        return text
    return f"{_COLORS.get(color, '')}{text}{_COLORS['reset']}"


def _score_color(score: float) -> str:
    """Return color name based on score value."""
    if score >= 80:
        return "green"
    elif score >= 60:
        return "yellow"
    else:
        return "red"


def _grade_label(score: float) -> str:
    if score >= 90:
        return "excellent"
    elif score >= 80:
        return "good"
    elif score >= 70:
        return "fair"
    elif score >= 60:
        return "needs improvement"
    else:
        return "poor"


def render_radar(score: SessionScore, use_color: bool = True) -> str:
    """Render a complete session health report with radar chart.

    Returns a multi-line string ready for terminal output.
    """
    axes = score.radar_axes  # {"SNR": x, "STATE": x, "CTX": x, "REACT": x}
    lines: List[str] = []

    W = 52  # box width

    # Header box
    lines.append(_c("╔" + "═" * W + "╗", "cyan", use_color))
    lines.append(_c("║", "cyan", use_color) + _c("  Session Health Report", "bold", use_color).ljust(W + (9 if use_color else 0)) + _c("║", "cyan", use_color))

    sid = score.session_id[:30] if score.session_id else "unknown"
    info = f"  ID: {sid}  Source: {score.source}"
    lines.append(_c("║", "cyan", use_color) + info.ljust(W) + _c("║", "cyan", use_color))

    info2 = f"  Model: {score.model or 'unknown'}  Turns: {score.turn_count}"
    lines.append(_c("║", "cyan", use_color) + info2.ljust(W) + _c("║", "cyan", use_color))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # Radar chart area
    chart_lines = _render_diamond(axes, use_color)
    for cl in chart_lines:
        padded = cl.ljust(W) if not use_color else cl + " " * max(0, W - _visible_len(cl))
        lines.append(_c("║", "cyan", use_color) + padded + _c("║", "cyan", use_color))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # Score summary
    comp_color = _score_color(score.composite)
    comp_str = f"  Session Score: {score.composite:.1f} / 100  ({_grade_label(score.composite)})"
    lines.append(_c("║", "cyan", use_color) + _c(comp_str, comp_color, use_color).ljust(W + (9 if use_color else 0)) + _c("║", "cyan", use_color))

    # Per-dimension breakdown
    dims = [
        ("STATE", score.state),
        ("SNR", score.snr),
        ("CTX", score.context),
        ("REACT", score.reaction),
    ]
    for i, (name, val) in enumerate(dims):
        prefix = "├─" if i < len(dims) - 1 else "└─"
        dc = _score_color(val)
        label = _grade_label(val)
        dim_str = f"  {prefix} {name:8s} {val:5.1f}  ({label})"
        lines.append(_c("║", "cyan", use_color) + _c(dim_str, dc, use_color).ljust(W + (9 if use_color else 0)) + _c("║", "cyan", use_color))

    # Stats line
    if score.composite_stddev > 0:
        stats = f"  σ={score.composite_stddev:.1f}  min={score.composite_min:.0f}  max={score.composite_max:.0f}"
        lines.append(_c("║", "cyan", use_color) + _c(stats, "dim", use_color).ljust(W + (9 if use_color else 0)) + _c("║", "cyan", use_color))

    if score.compaction_count > 0 or score.abort_count > 0:
        flags = f"  ⚠ compactions: {score.compaction_count}  aborts: {score.abort_count}"
        lines.append(_c("║", "cyan", use_color) + _c(flags, "yellow", use_color).ljust(W + (9 if use_color else 0)) + _c("║", "cyan", use_color))

    lines.append(_c("╚" + "═" * W + "╝", "cyan", use_color))

    return "\n".join(lines)


def _render_diamond(
    axes: Dict[str, float],
    use_color: bool,
) -> List[str]:
    """Render a 4-axis diamond/radar chart as text lines.

    Layout (21 cols wide, centered in 52-char box):
              SNR: 82
                ▲
               ╱ ╲
              ╱   ╲
             ╱  ●  ╲
    CTX: 65 ●───────● STATE: 91
             ╲  ●  ╱
              ╲   ╱
               ╲ ╱
                ▼
            REACT: 78
    """
    snr = axes.get("SNR", 0)
    state = axes.get("STATE", 0)
    react = axes.get("REACT", 0)
    ctx = axes.get("CTX", 0)

    # Normalize to 0–5 scale for the chart grid
    def norm(v: float) -> int:
        return max(0, min(5, round(v / 20)))

    n_snr = norm(snr)
    n_state = norm(state)
    n_react = norm(react)
    n_ctx = norm(ctx)

    lines: List[str] = []
    pad = "  "

    # Top label
    snr_label = f"SNR: {snr:.0f}"
    lines.append(pad + snr_label.center(48))

    # Build diamond grid (11 rows: 5 top + center + 5 bottom)
    grid_size = 5
    center = 24  # center column in 48-char space

    # Top half: row 0 (tip) to row 4
    lines.append(pad + " " * (center) + "▲")

    for row in range(1, grid_size):
        left_edge = center - row
        right_edge = center + row
        line = list(" " * 48)

        # Diamond edges
        line[left_edge] = "╱"
        line[right_edge] = "╲"

        # Fill point if within score range
        if row <= n_snr:
            line[center] = "·"

        lines.append(pad + "".join(line))

    # Center row
    center_line = list(" " * 48)
    # Left point (CTX)
    ctx_pos = center - grid_size
    center_line[ctx_pos] = "●"
    # Right point (STATE)
    state_pos = center + grid_size
    center_line[state_pos] = "●"
    # Horizontal line
    for c in range(ctx_pos + 1, state_pos):
        center_line[c] = "─"
    # Center marker
    center_line[center] = "●"

    # Add labels
    ctx_label = f"CTX:{ctx:.0f} "
    state_label = f" STATE:{state:.0f}"
    center_str = ctx_label + "".join(center_line)[len(ctx_label):48 - len(state_label)] + state_label
    lines.append(pad + center_str)

    # Bottom half: mirror of top
    for row in range(grid_size - 1, 0, -1):
        left_edge = center - row
        right_edge = center + row
        line = list(" " * 48)

        line[left_edge] = "╲"
        line[right_edge] = "╱"

        if row <= n_react:
            line[center] = "·"

        lines.append(pad + "".join(line))

    lines.append(pad + " " * (center) + "▼")

    # Bottom label
    react_label = f"REACT: {react:.0f}"
    lines.append(pad + react_label.center(48))

    return lines


def _visible_len(s: str) -> int:
    """Calculate visible length of string (excluding ANSI codes)."""
    import re
    return len(re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", s))


def render_table(score: SessionScore, use_color: bool = True) -> str:
    """Render a compact table summary."""
    lines = []
    lines.append(f"Session: {score.session_id}  ({score.source}, {score.model})")
    lines.append(f"Turns: {score.turn_count}  Score: {score.composite:.1f}/100 ({score.grade})")
    lines.append("-" * 50)
    lines.append(f"{'Dim':10s} {'Score':>6s}  {'Grade'}")
    lines.append("-" * 50)
    for name, val in score.radar_axes.items():
        lines.append(f"{name:10s} {val:6.1f}  {_grade_label(val)}")
    lines.append("-" * 50)
    return "\n".join(lines)


def render_json(score: SessionScore) -> str:
    """Render as JSON string."""
    import json
    return json.dumps({
        "session_id": score.session_id,
        "source": score.source,
        "model": score.model,
        "turn_count": score.turn_count,
        "composite": round(score.composite, 2),
        "grade": score.grade,
        "dimensions": {k: round(v, 2) for k, v in score.radar_axes.items()},
        "stats": {
            "min": round(score.composite_min, 2),
            "max": round(score.composite_max, 2),
            "stddev": round(score.composite_stddev, 2),
        },
        "events": {
            "compactions": score.compaction_count,
            "aborts": score.abort_count,
        },
    }, indent=2, ensure_ascii=False)
