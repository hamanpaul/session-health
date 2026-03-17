"""Agent-based session analysis.

Calls external CLI agents (Codex/Copilot/Gemini) to provide
AI-powered analysis and improvement suggestions for a session.

Agent priority (production):
  1. codex -c model=gpt-5.4 exec "prompt"
  2. copilot -s --model claude-sonnet-4.6 -p "prompt" --yolo
  3. gemini -m gemini-3-pro-preview -p "prompt"
  4. copilot -s --model gpt-5-mini -p "prompt" --yolo
"""

from __future__ import annotations

import html
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .scorer import SessionScore
from .parser_base import Session


@dataclass
class AgentConfig:
    """Configuration for an external agent CLI."""
    name: str
    build_cmd: "callable"  # (prompt: str) -> List[str]
    timeout: int = 120


def _build_codex_cmd(prompt: str) -> List[str]:
    return ["codex", "-c", "model=gpt-5.4", "exec", prompt]


def _build_copilot_sonnet_cmd(prompt: str) -> List[str]:
    return ["copilot", "-s", "--model", "claude-sonnet-4.6", "-p", prompt, "--yolo"]


def _build_gemini_cmd(prompt: str) -> List[str]:
    return ["gemini", "-m", "gemini-3-pro-preview", "-p", prompt]


def _build_copilot_mini_cmd(prompt: str) -> List[str]:
    return ["copilot", "-s", "--model", "gpt-5-mini", "-p", prompt, "--yolo"]


# Production agent chain
AGENT_CHAIN: List[AgentConfig] = [
    AgentConfig(name="codex/gpt-5.4", build_cmd=_build_codex_cmd, timeout=180),
    AgentConfig(name="copilot/sonnet-4.6", build_cmd=_build_copilot_sonnet_cmd, timeout=120),
    AgentConfig(name="gemini/3-pro", build_cmd=_build_gemini_cmd, timeout=120),
    AgentConfig(name="copilot/gpt-5-mini", build_cmd=_build_copilot_mini_cmd, timeout=90),
]

# Test-only: always use copilot mini
TEST_AGENT = AgentConfig(
    name="copilot/gpt-5-mini (test)",
    build_cmd=_build_copilot_mini_cmd,
    timeout=90,
)


@dataclass
class AgentAnalysis:
    """Result of an AI agent analysis."""
    agent_name: str = ""
    raw_response: str = ""
    success: bool = False
    error: str = ""


def prepare_analysis_prompt(
    score: SessionScore,
    session: Session,
    diagnosis_summary: Optional[Dict[str, Any]] = None,
    problemmap: Optional[Dict[str, Any]] = None,
    evidence_summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Build a prompt summarizing the session for agent analysis."""
    axes = score.radar_axes
    weak_dims = [f"{k}={v:.0f}" for k, v in axes.items() if v < 70]

    # Extract first 3 user messages
    user_msgs = []
    for t in session.turns[:5]:
        if t.user_input:
            msg = t.user_input[:200]
            if len(t.user_input) > 200:
                msg += "..."
            user_msgs.append(msg)
        if len(user_msgs) >= 3:
            break

    # Tool usage stats
    tool_counts: Dict[str, int] = {}
    tool_fails = 0
    for t in session.turns:
        for tc in t.tool_calls:
            tool_counts[tc.name] = tool_counts.get(tc.name, 0) + 1
            if tc.success is False or (tc.exit_code is not None and tc.exit_code != 0):
                tool_fails += 1

    tool_total = sum(tool_counts.values())
    top_tools = sorted(tool_counts.items(), key=lambda x: -x[1])[:5]
    tool_stats = ", ".join(f"{n}({c})" for n, c in top_tools)

    axes_str = " / ".join(f"{k}={v:.0f}" for k, v in axes.items())
    diagnosis_section = ""
    if diagnosis_summary:
        pm_lines = []
        for item in diagnosis_summary.get("pm_candidates", [])[:3]:
            pm_lines.append(
                f"- {item.get('field')}: {item.get('label_zh')}｜欄位意義={item.get('field_meaning_zh')}｜Fx={item.get('fx_weight_ratio_zh')}"
            )
        fx_lines = ", ".join(
            f"{item.get('fx')}={item.get('weight_pct')}"
            for item in diagnosis_summary.get("fx_weights", [])[:4]
            if float(item.get("weight", 0.0)) > 0
        ) or "無"
        weighted_dims = ", ".join(
            f"{item.get('dimension_zh')}={item.get('combined_attention_pct')}"
            for item in diagnosis_summary.get("weighted_dimensions", [])[:3]
        ) or "無"
        route = diagnosis_summary.get("route_summary", {})
        diagnosis_section = f"""

## 加權診斷摘要
- 摘要: {diagnosis_summary.get("summary_zh", "無")}
- 主家族: {route.get("primary_family_zh", "未解析")}
- 次家族: {route.get("secondary_family_zh", "無")}
- 破損不變量: {route.get("broken_invariant_zh", "尚未判定")}
- 優先修復方向: {route.get("first_fix_zh", "無")}
- Fx 權重: {fx_lines}
- 加權後最值得關注的維度: {weighted_dims}
{chr(10).join(pm_lines) if pm_lines else '- 無 PM 候選'}
"""
    elif problemmap:
        pm1_candidates = problemmap.get("pm1_candidates", [])
        pm1_str = ", ".join(
            f"{item.get('number')}:{item.get('label_zh', item.get('label'))}"
            for item in pm1_candidates[:3]
        ) or "無"
        atlas = problemmap.get("atlas", {})
        diagnosis_section = f"""

## ProblemMap / Atlas 診斷
- PM 候選: {pm1_str}
- 主家族: {atlas.get("primary_family_zh", atlas.get("primary_family", "未解析"))}
- 次家族: {atlas.get("secondary_family_zh", atlas.get("secondary_family", "無"))}
- 優先修復方向: {atlas.get("fix_surface_direction_zh", atlas.get("fix_surface_direction", "無"))}
"""

    evidence_section = ""
    if evidence_summary:
        weak_dim_str = ", ".join(
            f"{name}={value:.0f}"
            for name, value in evidence_summary.get("weak_dimensions", {}).items()
        ) or "無"
        top_turns = ", ".join(
            f"T{item.get('turn')}({item.get('composite')})"
            for item in evidence_summary.get("representative_turns", [])[:5]
        ) or "無"
        fail_str = ", ".join(evidence_summary.get("failed_tools", [])[:5]) or "無"
        signal_str = ", ".join(evidence_summary.get("candidate_failure_signals", [])[:5]) or "無"
        evidence_section = f"""

## 補充 Evidence 摘要
- Weak Dimensions: {weak_dim_str}
- Candidate Failure Signals: {signal_str}
- Representative Turns: {top_turns}
- Failed Tools: {fail_str}
"""

    prompt = f"""你是一個 Agent CLI Session 品質分析師。以下是一個 session 的評估摘要，請提供專業的改善建議。

## Session 基本資訊
- Session ID: {score.session_id}
- 來源: {score.source}, 模型: {score.model or 'unknown'}
- 回合數: {score.turn_count}
- 總分: {score.composite:.1f}/100 ({score.grade})

## 各維度分數
{axes_str}

## 低分維度（<70 分）
{', '.join(weak_dims) if weak_dims else '無（全部 ≥70）'}

## 前幾輪使用者訊息
{chr(10).join(f'{i+1}. {m}' for i, m in enumerate(user_msgs))}

## 工具使用統計
- 總呼叫數: {tool_total}, 失敗: {tool_fails}
- 常用工具: {tool_stats}

## 事件統計
- Abort 次數: {score.abort_count}
- Context Compaction 次數: {score.compaction_count}
{diagnosis_section}
{evidence_section}

## 請提供：
1. **整體評估**（2-3 句話概述此 session 的 prompt 品質）
2. **針對每個低分維度（<70 分）的具體改善建議**（如果有的話）
3. **最重要的 1 個改善行動**

用繁體中文回覆，簡潔扼要。使用 Markdown 格式。"""

    return prompt


def prepare_batch_analysis_prompt(
    aggregate: Dict[str, Any],
    session_summaries: List[Dict[str, Any]],
    diagnosis_summary: Optional[Dict[str, Any]] = None,
) -> str:
    """Build an analysis prompt for a batch of sessions."""

    session_lines = []
    for item in session_summaries[:12]:
        weak_dims = ", ".join(item.get("weak_dimensions", [])) or "無"
        session_lines.append(
            "- {session_id}: score={score} grade={grade} 主家族={primary} 弱項={weak} 修復方向={route}".format(
                session_id=item.get("session_id", "unknown"),
                score=item.get("score", "?"),
                grade=item.get("grade", "?"),
                primary=item.get("primary_family", "未解析"),
                weak=weak_dims,
                route=item.get("route", "無"),
            )
        )

    diagnosis_section = ""
    if diagnosis_summary:
        fx_lines = ", ".join(
            f"{item.get('fx')}={item.get('weight_pct')}"
            for item in diagnosis_summary.get("fx_weights", [])[:4]
            if float(item.get("weight", 0.0)) > 0
        ) or "無"
        dim_lines = ", ".join(
            f"{item.get('dimension_zh')}={item.get('combined_attention_pct')}"
            for item in diagnosis_summary.get("weighted_dimensions", [])[:3]
        ) or "無"
        diagnosis_section = f"""

## 批次加權診斷摘要
- 摘要: {diagnosis_summary.get('summary_zh', '無')}
- 主要 Fx 權重: {fx_lines}
- 加權後最值得關注的維度: {dim_lines}
"""

    prompt = f"""你是一個 Agent CLI Session 品質分析師。以下是一批 session 的彙整評估結果，請根據量化分數、ProblemMap / Atlas 診斷與 evidence 訊號，給出整體改善建議。

## Batch 概況
- Session 數量: {aggregate.get('session_count')}
- 平均分數: {aggregate.get('average_score')}
- 最低分數: {aggregate.get('min_score')}
- 最高分數: {aggregate.get('max_score')}
- 常見主家族: {', '.join(aggregate.get('primary_families', [])) or '無'}
- 常見 Failure Signals: {', '.join(aggregate.get('failure_signals', [])) or '無'}
{diagnosis_section}

## Session 摘要
{chr(10).join(session_lines)}

## 請提供：
1. **整體觀察**（2-3 句）
2. **最常見的結構性問題模式**（聚焦 ProblemMap / Atlas）
3. **最值得優先做的 1-2 個工程改善行動**

用繁體中文回覆，簡潔扼要。使用 Markdown 格式。"""

    return prompt


def call_agent(
    prompt: str,
    agent_chain: Optional[List[AgentConfig]] = None,
    test_mode: bool = False,
) -> AgentAnalysis:
    """Call an external agent CLI, trying the chain in order.

    Args:
        prompt: The analysis prompt to send
        agent_chain: Custom agent chain (default: AGENT_CHAIN)
        test_mode: If True, only use TEST_AGENT
    """
    if test_mode:
        agents = [TEST_AGENT]
    else:
        agents = agent_chain or AGENT_CHAIN

    for agent in agents:
        # Check if the CLI tool is available
        cmd_name = agent.build_cmd("test")[0]
        if not shutil.which(cmd_name):
            continue

        cmd = agent.build_cmd(prompt)

        try:
            print(f"  🤖 Calling {agent.name}...", file=sys.stderr, end="", flush=True)
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=agent.timeout,
                env={**os.environ, "NO_COLOR": "1"},
            )

            output = result.stdout.strip()
            if result.returncode == 0 and output:
                print(f" ✓", file=sys.stderr)
                return AgentAnalysis(
                    agent_name=agent.name,
                    raw_response=output,
                    success=True,
                )
            else:
                err = result.stderr.strip()[:200]
                print(f" ✗ (exit={result.returncode})", file=sys.stderr)

        except subprocess.TimeoutExpired:
            print(f" ✗ (timeout)", file=sys.stderr)
        except Exception as e:
            print(f" ✗ ({e})", file=sys.stderr)

    return AgentAnalysis(
        success=False,
        error="All agents in the chain failed or are unavailable.",
    )


def render_agent_html_section(analysis: AgentAnalysis) -> str:
    """Render the agent analysis as an HTML section for the report."""
    if not analysis.success:
        return ""

    # Convert markdown-ish content to basic HTML
    content = analysis.raw_response
    content_html = _markdown_to_html(content)

    return f"""
    <div class="agent-analysis">
        <h2>🤖 AI 分析報告</h2>
        <div class="agent-meta">
            分析引擎: <strong>{html.escape(analysis.agent_name)}</strong>
        </div>
        <div class="agent-content">
            {content_html}
        </div>
    </div>
    """


def render_agent_terminal(analysis: AgentAnalysis) -> str:
    """Render agent analysis for terminal output."""
    if not analysis.success:
        return ""

    lines = [
        "",
        "╔════════════════════════════════════════════════════════╗",
        f"║  🤖 AI Analysis (via {analysis.agent_name})",
        "╠════════════════════════════════════════════════════════╣",
    ]
    # Wrap response text to fit box
    for line in analysis.raw_response.split("\n"):
        if line.strip():
            # Truncate long lines
            if len(line) > 54:
                lines.append(f"║  {line[:52]}…")
            else:
                lines.append(f"║  {line}")
    lines.append("╚════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def _markdown_to_html(md: str) -> str:
    """Very basic markdown-to-HTML conversion (no external deps)."""
    import re
    lines = md.split("\n")
    out: List[str] = []
    in_list = False

    for line in lines:
        stripped = line.strip()
        if not stripped:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append("<br>")
            continue

        # Headers
        if stripped.startswith("### "):
            out.append(f"<h4>{html.escape(stripped[4:])}</h4>")
        elif stripped.startswith("## "):
            out.append(f"<h3>{html.escape(stripped[3:])}</h3>")
        elif stripped.startswith("# "):
            out.append(f"<h3>{html.escape(stripped[2:])}</h3>")
        # List items
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                out.append("<ul>")
                in_list = True
            item = stripped[2:]
            # Bold
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            out.append(f"<li>{item}</li>")
        # Numbered list
        elif re.match(r'^\d+\.\s', stripped):
            if not in_list:
                out.append("<ol>")
                in_list = True
            item = re.sub(r'^\d+\.\s', '', stripped)
            item = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', item)
            out.append(f"<li>{item}</li>")
        else:
            if in_list:
                # Check if prev was <ol> or <ul>
                for prev in reversed(out):
                    if "<ol>" in prev:
                        out.append("</ol>")
                        break
                    elif "<ul>" in prev:
                        out.append("</ul>")
                        break
                in_list = False
            # Bold inline
            processed = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', html.escape(stripped))
            out.append(f"<p>{processed}</p>")

    if in_list:
        out.append("</ul>")

    return "\n".join(out)
