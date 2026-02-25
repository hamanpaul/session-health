"""Terminal renderer for session health reports.

RPG-style progress-bar display for 6 evaluation dimensions:
  SNR    (信噪比)    — 終端輸出中無效雜訊的佔比，越低越好
  STATE  (狀態完整度) — 環境關鍵資訊（cwd、exit code、權限）的覆蓋率
  REACT  (反應指標)   — LLM 是否出現死迴圈、解析錯誤等異常反應
  CONV   (收斂力)    — 任務是否順利完成、是否中途 abort/重啟
  CTX    (記憶留存)   — 多輪對話中原始目標是否被遺忘、歷史 log 冗餘度
  DEPTH  (推理深度)   — Agent 推理的密度與品質，是否先思考再行動

Supports ANSI colors (can be disabled with --no-color).
"""

from __future__ import annotations

from typing import Dict, List

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
    "bg_red": "\033[41m",
    "bg_green": "\033[42m",
    "bg_yellow": "\033[43m",
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


# Dimension zh-TW descriptions
_DIM_LABELS = {
    "SNR":   "信噪比",
    "STATE": "狀態完整度",
    "REACT": "反應指標",
    "CONV":  "收斂力",
    "CTX":   "記憶留存",
    "DEPTH": "推理深度",
}

# Short descriptions for terminal display
_DIM_DESC = {
    "SNR":   "雜訊過濾品質",
    "STATE": "環境狀態覆蓋",
    "REACT": "模型反應正常",
    "CONV":  "任務收斂程度",
    "CTX":   "上下文記憶力",
    "DEPTH": "推理深度品質",
}

BAR_WIDTH = 25  # characters for the bar


def _make_bar(score: float, use_color: bool) -> str:
    """Build an RPG-style HP bar: ████████░░░░░░░░"""
    filled = round(score * BAR_WIDTH / 100)
    empty = BAR_WIDTH - filled
    bar_fill = "█" * filled
    bar_empty = "░" * empty
    color = _score_color(score)
    return _c(bar_fill, color, use_color) + _c(bar_empty, "dim", use_color)


def render_radar(score: SessionScore, use_color: bool = True) -> str:
    """Render a session health report with RPG-style progress bars."""
    axes = score.radar_axes
    lines: List[str] = []

    W = 56  # box inner width

    def box(content: str) -> str:
        return _c("║", "cyan", use_color) + _pad_to(content, W, use_color) + _c("║", "cyan", use_color)

    # ── Header ──
    lines.append(_c("╔" + "═" * W + "╗", "cyan", use_color))
    lines.append(box(_c("  ⚔ Session Health Report", "bold", use_color)))
    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    sid = score.session_id[:38] if score.session_id else "unknown"
    lines.append(box(f"  ID:     {sid}"))
    lines.append(box(f"  Source: {score.source}    Model: {score.model or 'unknown'}    Turns: {score.turn_count}"))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # ── Overall score bar ──
    comp_grade = f"{score.composite:.1f}/100 ({score.grade})"
    comp_color = _score_color(score.composite)
    lines.append(box(""))
    lines.append(box(_c(f"  Overall Score: {comp_grade}", comp_color, use_color)))
    lines.append(box(f"  {_make_bar(score.composite, use_color)}"))
    lines.append(box(""))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # ── Per-dimension bars ──
    lines.append(box(""))
    dim_order = ["SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV"]
    for name in dim_order:
        val = axes[name]
        zh = _DIM_LABELS[name]
        desc = _DIM_DESC[name]
        color = _score_color(val)

        # Line 1: label + description
        # "  SNR  信噪比     雜訊過濾品質"
        label_part = f"  {name:6s}{zh}"
        lines.append(box(_c(label_part, "bold", use_color)))

        # Line 2: bar + score
        bar = _make_bar(val, use_color)
        score_str = _c(f"{val:5.1f}", color, use_color)
        lines.append(box(f"  {bar} {score_str}  {_c(desc, 'dim', use_color)}"))
        lines.append(box(""))

    # ── Footer stats ──
    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))
    if score.composite_stddev > 0:
        stats = f"  σ={score.composite_stddev:.1f}  min={score.composite_min:.0f}  max={score.composite_max:.0f}"
        lines.append(box(_c(stats, "dim", use_color)))
    if score.compaction_count > 0 or score.abort_count > 0:
        flags = f"  ⚠ compactions: {score.compaction_count}  aborts: {score.abort_count}"
        lines.append(box(_c(flags, "yellow", use_color)))
    if score.composite_stddev <= 0 and score.compaction_count == 0 and score.abort_count == 0:
        lines.append(box(_c("  ✓ No anomalies detected", "green", use_color)))

    lines.append(_c("╚" + "═" * W + "╝", "cyan", use_color))

    return "\n".join(lines)


def _visible_len(s: str) -> int:
    """Calculate visible terminal width (excluding ANSI codes, CJK=2 cols)."""
    import re
    import unicodedata
    clean = re.sub(r"\033\[[0-9;]*[a-zA-Z]", "", s)
    width = 0
    for ch in clean:
        eaw = unicodedata.east_asian_width(ch)
        width += 2 if eaw in ("W", "F") else 1
    return width


def _pad_to(s: str, target_width: int, use_color: bool = False) -> str:
    """Pad string to target terminal width, accounting for CJK chars."""
    vlen = _visible_len(s)
    pad = max(0, target_width - vlen)
    return s + " " * pad


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
