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

from dataclasses import asdict
from typing import Dict, List

from .agent_analysis import render_agent_terminal
from .report_types import BatchReport, DiagnosisSummary, ProblemMapDiagnosis, SessionReport
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
    "TOOL":  "工具效率",
}

# Short descriptions for terminal display
_DIM_DESC = {
    "SNR":   "雜訊過濾品質",
    "STATE": "環境狀態覆蓋",
    "REACT": "模型反應正常",
    "CONV":  "任務收斂程度",
    "CTX":   "上下文記憶力",
    "DEPTH": "推理深度品質",
    "TOOL":  "工具使用效率",
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


def _unwrap_score(item: SessionScore | SessionReport) -> SessionScore:
    if isinstance(item, SessionReport):
        return item.score
    return item


def render_radar(item: SessionScore | SessionReport, use_color: bool = True) -> str:
    """Render a session health report with RPG-style progress bars."""
    score = _unwrap_score(item)
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
    dim_order = ["SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL"]
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


def render_problemmap_terminal(
    diagnosis: ProblemMapDiagnosis | None,
    use_color: bool = True,
) -> str:
    """Render a compact ProblemMap / Atlas diagnosis box."""

    if diagnosis is None:
        return ""

    atlas = diagnosis.atlas
    lines: List[str] = [
        _c("╔" + "═" * 56 + "╗", "magenta", use_color),
        _c("║", "magenta", use_color) + _pad_to(_c("  🧭 ProblemMap 診斷", "bold", use_color), 56, use_color) + _c("║", "magenta", use_color),
        _c("╠" + "═" * 56 + "╣", "magenta", use_color),
    ]

    def add_line(prefix: str, value: str) -> None:
        content = f"  {prefix}: {value}".strip()
        for chunk in _wrap_visible(content, 54):
            lines.append(_c("║", "magenta", use_color) + _pad_to(f" {chunk}", 56, use_color) + _c("║", "magenta", use_color))

    pm1 = ", ".join(
        f"{item.get('number')}:{item.get('label_zh', item.get('label'))}"
        for item in diagnosis.pm1_candidates[:3]
    ) or "無"
    add_line("PM1", pm1)
    add_line("主家族", str(atlas.get("primary_family_zh", atlas.get("primary_family", "未解析"))))
    add_line("次家族", str(atlas.get("secondary_family_zh", atlas.get("secondary_family", "無"))))
    add_line("破損不變量", str(atlas.get("broken_invariant_zh", atlas.get("broken_invariant", "尚未判定"))))
    add_line("優先修復方向", str(atlas.get("fix_surface_direction_zh", atlas.get("fix_surface_direction", "無"))))
    add_line("信心 / 證據", f"{atlas.get('confidence_zh', atlas.get('confidence', '低'))} / {atlas.get('evidence_sufficiency_zh', atlas.get('evidence_sufficiency', '薄弱'))}")

    if diagnosis.need_more_evidence:
        add_line("備註", "目前仍需更多證據，再做強修復判斷。")

    lines.append(_c("╚" + "═" * 56 + "╝", "magenta", use_color))
    return "\n".join(lines)


def render_diagnosis_summary_terminal(
    summary: DiagnosisSummary | None,
    use_color: bool = True,
) -> str:
    """Render the integrated weighted diagnosis section."""

    if summary is None:
        return ""

    lines: List[str] = [
        _c("╔" + "═" * 56 + "╗", "magenta", use_color),
        _c("║", "magenta", use_color) + _pad_to(_c("  🧭 加權診斷摘要", "bold", use_color), 56, use_color) + _c("║", "magenta", use_color),
        _c("╠" + "═" * 56 + "╣", "magenta", use_color),
    ]

    def add_line(prefix: str, value: str) -> None:
        content = f"  {prefix}: {value}".strip()
        for chunk in _wrap_visible(content, 54):
            lines.append(_c("║", "magenta", use_color) + _pad_to(f" {chunk}", 56, use_color) + _c("║", "magenta", use_color))

    add_line("摘要", summary.summary_zh or "無")
    route = summary.route_summary
    if route:
        primary = route.get("primary_family_zh") or "未解析"
        secondary = route.get("secondary_family_zh") or "無"
        if primary != "未解析" or summary.scope == "batch":
            add_line("主 / 次家族", f"{primary} / {secondary}")
        if route.get("broken_invariant_zh"):
            add_line("破損不變量", str(route.get("broken_invariant_zh")))
        if route.get("first_fix_zh"):
            add_line("優先修復方向", str(route.get("first_fix_zh")))

    top_fx = "、".join(
        f"{item.get('fx')} {item.get('weight_pct')}"
        for item in summary.fx_weights[:4]
        if float(item.get("weight", 0.0)) > 0
    ) or "無顯著 Fx 權重"
    add_line("Fx 權重", top_fx)

    top_dims = "、".join(
        f"{item.get('dimension_zh')} {item.get('combined_attention_pct')}"
        for item in summary.weighted_dimensions[:3]
    ) or "無"
    add_line("加權關注維度", top_dims)

    for item in summary.pm_candidates[:3]:
        add_line(
            item.get("field", "PM"),
            f"{item.get('label_zh')}｜{item.get('field_meaning_zh')}",
        )
        add_line(f"{item.get('field', 'PM')} Fx", item.get("fx_weight_ratio_zh", "無"))

    lines.append(_c("╚" + "═" * 56 + "╝", "magenta", use_color))
    return "\n".join(lines)


def render_report_terminal(report: SessionReport, use_color: bool = True) -> str:
    """Render the terminal bundle for a single session report."""

    parts = [render_radar(report, use_color)]
    diagnosis_box = render_diagnosis_summary_terminal(report.diagnosis_summary, use_color)
    if diagnosis_box:
        parts.append(diagnosis_box)
    else:
        problemmap_box = render_problemmap_terminal(report.problemmap, use_color)
        if problemmap_box:
            parts.append(problemmap_box)
    if report.agent_analysis is not None and report.agent_analysis.success:
        parts.append(render_agent_terminal(report.agent_analysis))
    return "\n".join(part for part in parts if part)


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


def render_table(item: SessionScore | SessionReport | BatchReport, use_color: bool = True) -> str:
    """Render a compact table summary."""
    if isinstance(item, BatchReport):
        lines = []
        lines.append(f"Batch: {len(item.sessions)} sessions  Target: {item.target_kind}")
        lines.append("-" * 88)
        lines.append(f"{'Session':24s} {'Score':>7s} {'Grade':6s} {'主家族'}")
        lines.append("-" * 88)
        for report in item.sessions:
            primary = "未解析"
            if report.problemmap is not None:
                primary = str(report.problemmap.atlas.get("primary_family_zh", report.problemmap.atlas.get("primary_family", "未解析")))
            session_id = (report.score.session_id or "unknown")[:24]
            lines.append(f"{session_id:24s} {report.score.composite:7.1f} {report.score.grade:6s} {primary}")
        lines.append("-" * 88)
        return "\n".join(lines)

    score = _unwrap_score(item)
    lines = []
    lines.append(f"Session: {score.session_id}  ({score.source}, {score.model})")
    lines.append(f"Turns: {score.turn_count}  Score: {score.composite:.1f}/100 ({score.grade})")
    if isinstance(item, SessionReport):
        if item.diagnosis_summary is not None:
            lines.append(f"加權診斷: {item.diagnosis_summary.summary_zh}")
        elif item.problemmap is not None:
            lines.append(f"ProblemMap 主家族: {item.problemmap.atlas.get('primary_family_zh', item.problemmap.atlas.get('primary_family', '未解析'))}")
    lines.append("-" * 50)
    lines.append(f"{'Dim':10s} {'Score':>6s}  {'Grade'}")
    lines.append("-" * 50)
    for name, val in score.radar_axes.items():
        lines.append(f"{name:10s} {val:6.1f}  {_grade_label(val)}")
    lines.append("-" * 50)
    return "\n".join(lines)


def render_json(item: SessionScore | SessionReport | BatchReport) -> str:
    """Render as JSON string."""
    import json
    if isinstance(item, BatchReport):
        payload = {
            "report_kind": item.report_kind,
            "target_kind": item.target_kind,
            "analysis_layers": item.analysis_layers,
            "sync_status": item.sync_status,
            "diagnosis_summary": asdict(item.diagnosis_summary) if item.diagnosis_summary is not None else None,
            "evidence_summary": item.evidence_summary,
            "artifact_sources": item.artifact_sources,
            "agent_analysis": {
                "agent_name": item.agent_analysis.agent_name,
                "success": item.agent_analysis.success,
                "raw_response": item.agent_analysis.raw_response,
                "error": item.agent_analysis.error,
            }
            if item.agent_analysis is not None
            else None,
            "sessions": [
                json.loads(render_json(report))
                for report in item.sessions
            ],
        }
        return json.dumps(payload, indent=2, ensure_ascii=False)

    score = _unwrap_score(item)
    payload = {
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
    }
    if isinstance(item, SessionReport):
        payload.update(
            {
                "report_kind": item.report_kind,
                "target_kind": item.target_kind,
                "analysis_layers": item.analysis_layers,
                "sync_status": item.sync_status,
                "diagnosis_summary": asdict(item.diagnosis_summary) if item.diagnosis_summary is not None else None,
                "evidence_summary": item.evidence_summary,
                "artifact_sources": item.artifact_sources,
                "problemmap": {
                    "status": item.problemmap.status,
                    "diagnostic_mode": item.problemmap.diagnostic_mode,
                    "pm1_candidates": item.problemmap.pm1_candidates,
                    "fx_weights": item.problemmap.fx_weights,
                    "atlas": item.problemmap.atlas,
                    "global_fix_route": item.problemmap.global_fix_route,
                    "references_used": item.problemmap.references_used,
                    "source_case": item.problemmap.source_case,
                    "need_more_evidence": item.problemmap.need_more_evidence,
                }
                if item.problemmap is not None
                else None,
                "agent_analysis": {
                    "agent_name": item.agent_analysis.agent_name,
                    "success": item.agent_analysis.success,
                    "raw_response": item.agent_analysis.raw_response,
                    "error": item.agent_analysis.error,
                }
                if item.agent_analysis is not None
                else None,
            }
        )
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _wrap_visible(text: str, width: int) -> List[str]:
    """Wrap plain text to a visible terminal width."""

    words = text.split()
    if not words:
        return [""]
    lines: List[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _visible_len(candidate) <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines
