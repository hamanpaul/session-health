"""Text-based radar chart renderer.

Renders a 6-axis hexagonal radar chart using Unicode characters.
Axes (clockwise from top):
  SNR    (信噪比)    — 終端輸出中無效雜訊的佔比，越低越好
  STATE  (狀態完整度) — 環境關鍵資訊（cwd、exit code、權限）的覆蓋率
  REACT  (反應指標)   — LLM 是否出現死迴圈、解析錯誤等異常反應
  CONV   (收斂力)    — 任務是否順利完成、是否中途 abort/重啟
  CTX    (記憶留存)   — 多輪對話中原始目標是否被遺忘、歷史 log 冗餘度
  DEPTH  (推理深度)   — Agent 推理的密度與品質，是否先思考再行動

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


# Dimension zh-TW descriptions
_DIM_LABELS = {
    "SNR":   "信噪比",
    "STATE": "狀態完整度",
    "REACT": "反應指標",
    "CONV":  "收斂力",
    "CTX":   "記憶留存",
    "DEPTH": "推理深度",
}


def render_radar(score: SessionScore, use_color: bool = True) -> str:
    """Render a complete session health report with 6-axis radar chart."""
    axes = score.radar_axes
    lines: List[str] = []

    W = 56  # box width

    # Header box
    lines.append(_c("╔" + "═" * W + "╗", "cyan", use_color))

    title = _c("  Session Health Report", "bold", use_color)
    lines.append(_c("║", "cyan", use_color) + _pad_to(title, W, use_color) + _c("║", "cyan", use_color))

    sid = score.session_id[:34] if score.session_id else "unknown"
    info = f"  ID: {sid}  Source: {score.source}"
    lines.append(_c("║", "cyan", use_color) + _pad_to(info, W) + _c("║", "cyan", use_color))

    info2 = f"  Model: {score.model or 'unknown'}  Turns: {score.turn_count}"
    lines.append(_c("║", "cyan", use_color) + _pad_to(info2, W) + _c("║", "cyan", use_color))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # Radar chart area
    chart_lines = _render_hexagon(axes, use_color)
    for cl in chart_lines:
        lines.append(_c("║", "cyan", use_color) + _pad_to(cl, W) + _c("║", "cyan", use_color))

    lines.append(_c("╠" + "═" * W + "╣", "cyan", use_color))

    # Score summary
    comp_color = _score_color(score.composite)
    comp_str = _c(f"  Session Score: {score.composite:.1f} / 100  ({_grade_label(score.composite)})", comp_color, use_color)
    lines.append(_c("║", "cyan", use_color) + _pad_to(comp_str, W, use_color) + _c("║", "cyan", use_color))

    # Per-dimension breakdown with zh-TW labels
    dim_list = list(axes.items())
    for i, (name, val) in enumerate(dim_list):
        prefix = "├─" if i < len(dim_list) - 1 else "└─"
        dc = _score_color(val)
        zh = _DIM_LABELS.get(name, "")
        dim_str = _c(f"  {prefix} {name:6s} {val:5.1f}  {zh}({_grade_label(val)})", dc, use_color)
        lines.append(_c("║", "cyan", use_color) + _pad_to(dim_str, W, use_color) + _c("║", "cyan", use_color))

    # Stats line
    if score.composite_stddev > 0:
        stats = _c(f"  σ={score.composite_stddev:.1f}  min={score.composite_min:.0f}  max={score.composite_max:.0f}", "dim", use_color)
        lines.append(_c("║", "cyan", use_color) + _pad_to(stats, W, use_color) + _c("║", "cyan", use_color))

    if score.compaction_count > 0 or score.abort_count > 0:
        flags = _c(f"  ⚠ compactions: {score.compaction_count}  aborts: {score.abort_count}", "yellow", use_color)
        lines.append(_c("║", "cyan", use_color) + _pad_to(flags, W, use_color) + _c("║", "cyan", use_color))

    lines.append(_c("╚" + "═" * W + "╝", "cyan", use_color))

    return "\n".join(lines)


def _render_hexagon(
    axes: Dict[str, float],
    use_color: bool,
) -> List[str]:
    """Render a proper 6-vertex hexagonal radar chart.

    Layout (clockwise from top):
                     SNR
                      ▲
                    ╱   ╲
                  ╱       ╲
      DEPTH:66  ●─────●─────●  STATE:100
                │     ·     │
                │   · ● ·   │
                │     ·     │
      CTX:30    ●─────●─────●  REACT:100
                  ╲       ╱
                    ╲   ╱
                      ▼
                     CONV
    """
    axis_names = ["SNR", "STATE", "REACT", "CONV", "CTX", "DEPTH"]
    vals = {k: axes.get(k, 0) for k in axis_names}

    CW = 54  # content width (+ "  " prefix = 56 = box W)
    cx = CW // 2  # 27
    HW = 9  # half-width from center to side vertices
    lv = cx - HW  # 18  left vertex column
    rv = cx + HW  # 36  right vertex column

    lines: List[str] = []

    # ── SNR label (top, centered) ──
    snr_lbl = f"SNR({_DIM_LABELS['SNR']}): {vals['SNR']:.0f}"
    snr_pad = (CW - _visible_len(snr_lbl)) // 2
    lines.append("  " + " " * snr_pad + snr_lbl)

    # ── Top vertex ▲ ──
    lines.append("  " + " " * cx + "▲")

    # ── Upper diagonal (2 rows, expanding toward upper vertices) ──
    diag_offsets = [3, 6]
    for off in diag_offsets:
        row = list(" " * CW)
        row[cx - off] = "╱"
        row[cx + off] = "╲"
        if vals["SNR"] >= (60 if off == 3 else 30):
            row[cx] = "·"
        lines.append("  " + "".join(row))

    # ── Upper vertex row: DEPTH ●─────●─────● STATE ──
    def _vertex_row(left_label: str, right_label: str) -> str:
        row = list(" " * CW)
        row[lv] = "●"
        row[rv] = "●"
        row[cx] = "●"
        for c in range(lv + 1, cx):
            row[c] = "─"
        for c in range(cx + 1, rv):
            row[c] = "─"
        # Overlay left label (right-justified before vertex)
        start_l = lv - len(left_label) - 1
        for i, ch in enumerate(left_label):
            if 0 <= start_l + i < lv:
                row[start_l + i] = ch
        # Overlay right label (left-justified after vertex)
        start_r = rv + 2
        for i, ch in enumerate(right_label):
            if start_r + i < CW:
                row[start_r + i] = ch
        return "  " + "".join(row)

    depth_lbl = f"DEPTH:{vals['DEPTH']:.0f}"
    state_lbl = f"STATE:{vals['STATE']:.0f}"
    lines.append(_vertex_row(depth_lbl, state_lbl))

    # ── Middle vertical section (3 rows with │ sides) ──
    for m in range(3):
        row = list(" " * CW)
        row[lv] = "│"
        row[rv] = "│"
        if m == 1:
            # Center row: ● and diagonal axis hints
            row[cx] = "●"
            if vals["DEPTH"] >= 50:
                row[cx - 4] = "·"
            if vals["STATE"] >= 50:
                row[cx + 4] = "·"
            if vals["CTX"] >= 50:
                row[cx - 4] = "·"
            if vals["REACT"] >= 50:
                row[cx + 4] = "·"
        else:
            # Vertical axis dot
            row[cx] = "·"
        lines.append("  " + "".join(row))

    # ── Lower vertex row: CTX ●─────●─────● REACT ──
    ctx_lbl = f"CTX:{vals['CTX']:.0f}"
    react_lbl = f"REACT:{vals['REACT']:.0f}"
    lines.append(_vertex_row(ctx_lbl, react_lbl))

    # ── Lower diagonal (2 rows, contracting toward bottom vertex) ──
    for off in reversed(diag_offsets):
        row = list(" " * CW)
        row[cx - off] = "╲"
        row[cx + off] = "╱"
        if vals["CONV"] >= (60 if off == 3 else 30):
            row[cx] = "·"
        lines.append("  " + "".join(row))

    # ── Bottom vertex ▼ ──
    lines.append("  " + " " * cx + "▼")

    # ── CONV label (bottom, centered) ──
    conv_lbl = f"CONV({_DIM_LABELS['CONV']}): {vals['CONV']:.0f}"
    conv_pad = (CW - _visible_len(conv_lbl)) // 2
    lines.append("  " + " " * conv_pad + conv_lbl)

    return lines


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
