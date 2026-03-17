"""Single-file HTML report generator with SVG hexagonal radar chart.

Generates a self-contained HTML file (no external dependencies) with:
- SVG hexagonal radar chart
- Per-metric progress bars
- Detailed descriptions and improvement suggestions for each dimension
"""

from __future__ import annotations

import html
import json
import math
from typing import Dict, List, Tuple

from .agent_analysis import render_agent_html_section
from .report_types import BatchReport, DiagnosisSummary, ProblemMapDiagnosis, SessionReport
from .scorer import SessionScore

# ── Dimension metadata ──

DIM_ORDER = ["SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL"]

DIM_META = {
    "SNR": {
        "name": "信噪比",
        "en": "Signal-to-Noise Ratio",
        "icon": "📡",
        "color": "#4fc3f7",
        "description": (
            "衡量動態 Prompt 中「有效資訊」與「無效雜訊」的比率。"
            "包含 ANSI 逃脫序列、安裝進度條、重複超過 N 次的相似錯誤行等。"
            "比例越低（分數越高），代表雜訊過濾與截斷機制越優秀。"
        ),
        "what_measured": [
            "ANSI 逃脫碼與控制字元佔比",
            "進度條、spinner 等非語義輸出偵測",
            "重複行（duplicate lines）壓縮比率",
            "無效 token 佔總 token 的百分比",
        ],
        "suggestions": {
            "excellent": "雜訊過濾表現優異，Prompt 中幾乎沒有無效內容。維持現有的輸出截斷策略。",
            "good": "輕微雜訊存在但不影響模型判斷。可考慮增加 ANSI strip 或 tail 截斷長度限制。",
            "fair": "有一定比例的無效內容進入 Prompt。建議檢查是否有未過濾的安裝日誌或編譯警告。",
            "needs_improvement": "雜訊佔比偏高。建議：(1) 加強 ANSI 碼清理 (2) 對重複行做 dedup (3) 限制 tool output 的最大 token 數。",
            "poor": "大量無效內容佔用 Context Window。立即檢查：是否將完整的 npm install / pip install 輸出直接送入 Prompt？需實作嚴格的輸出摘要機制。",
        },
    },
    "STATE": {
        "name": "狀態完整度",
        "en": "State Integrity",
        "icon": "🗂️",
        "color": "#81c784",
        "description": (
            "檢查每次發送給 LLM 的 Prompt 是否穩定包含模型決策必備的環境資訊。"
            "缺失關鍵狀態（如當前路徑、上一步退出碼）會導致模型做出錯誤判斷。"
        ),
        "what_measured": [
            "當前工作目錄 (pwd/cwd) 是否存在",
            "上一步指令的退出碼 (Exit Code) 是否回傳",
            "當前使用者權限 (root/sudo) 是否標示",
            "Git 狀態（分支、是否有未提交變更）資訊",
        ],
        "suggestions": {
            "excellent": "環境狀態資訊完整，模型能精確掌握執行上下文。繼續保持。",
            "good": "大部分狀態資訊完整，少數 turn 可能缺少 git 狀態。影響不大。",
            "fair": "部分關鍵資訊缺失。建議確認 tool execution 的 output 是否包含 cwd 和 exit code。",
            "needs_improvement": "狀態覆蓋率偏低。檢查：(1) 是否在每次指令執行後回傳 exit code (2) 是否在 system prompt 中注入 cwd。",
            "poor": "嚴重缺乏環境上下文。模型在「盲目」執行。需在 CLI framework 層級確保每次 turn 都注入基礎環境狀態。",
        },
    },
    "CTX": {
        "name": "記憶留存",
        "en": "Context Memory",
        "icon": "🧠",
        "color": "#ffb74d",
        "description": (
            "在多輪長對話中，檢驗使用者的「原始核心任務」是否被長篇 Log 擠出 Context Window。"
            "同時計算歷史指令區塊中，未經摘要的原始 Log 佔比。"
            "好的 Prompt 應具備動態摘要能力，保留核心意圖。"
        ),
        "what_measured": [
            "原始任務關鍵詞在後續 turn 中的留存率",
            "關鍵詞在 Prompt 中的位置（越前面權重越高）",
            "未經壓縮的原始 log 佔 Context Window 比例",
            "Context compaction 事件的頻率",
        ],
        "suggestions": {
            "excellent": "核心任務關鍵詞在整個 session 中穩定保留。摘要策略極佳。",
            "good": "大部分情況下能保留核心意圖。少數長 log 後可能有輕微稀釋。",
            "fair": "隨著對話增長，原始目標開始被稀釋。建議：在 compaction 時優先保留第一條 user message 的完整內容。",
            "needs_improvement": "核心任務資訊已大幅流失。建議：(1) 在 system prompt 中固定一個 「mission」 區塊 (2) 實作 progressive summarization (3) 避免將完整 tool output 存入歷史。",
            "poor": "模型已基本遺忘原始任務。需要重新架構 context management：(1) 將 goal 設為 pinned 區塊 (2) 限制每輪保留的歷史長度 (3) 對歷史 log 做強制摘要。",
        },
    },
    "REACT": {
        "name": "反應指標",
        "en": "LLM Reaction Quality",
        "icon": "⚡",
        "color": "#e57373",
        "description": (
            "最直觀的反向指標。如果 LLM 收到 Prompt 後頻繁輸出無法被系統解析的內容，"
            "或不斷重複輸入完全相同的錯誤指令（死迴圈），代表動態 Prompt 已混亂，"
            "導致模型失去焦點。"
        ),
        "what_measured": [
            "連續重複相同指令的次數（Loop Rate）",
            "被系統中斷/abort 的 turn 比率",
            "模型輸出中解析錯誤的頻率",
            "連續失敗後是否能自主調整策略",
        ],
        "suggestions": {
            "excellent": "模型反應精準，無死迴圈或解析錯誤。Prompt 品質優異。",
            "good": "偶有重複但能自主修正。整體反應品質良好。",
            "fair": "出現部分迴圈行為。建議：檢查是否有 tool output 格式不一致導致模型誤判。",
            "needs_improvement": "明顯的迴圈或 abort 行為。建議：(1) 在 Prompt 中加入「如果此方法失敗，嘗試替代方案」的指引 (2) 限制相同指令的最大重試次數。",
            "poor": "嚴重的死迴圈或大量 abort。Prompt 結構需要重大改進：(1) 加入明確的 error recovery 指引 (2) 在連續失敗時自動注入 diagnostic context (3) 考慮 cooldown 機制。",
        },
    },
    "DEPTH": {
        "name": "推理深度",
        "en": "Reasoning Depth",
        "icon": "🔬",
        "color": "#ba68c8",
        "description": (
            "衡量 Agent 在執行動作前是否有充分的推理過程。"
            "好的 Agent 應該「先思考再行動」— 在呼叫 tool 之前有 reasoning 區塊，"
            "而非直接盲目執行指令。推理密度越高，代表 Prompt 成功引導模型進行深度分析。"
        ),
        "what_measured": [
            "包含 reasoning/thinking 區塊的 turn 比率",
            "推理區塊與 tool call 的比率（reasoning density）",
            "推理內容的長度是否足夠（非單行敷衍）",
            "是否在執行前有明確的計畫或分析",
        ],
        "suggestions": {
            "excellent": "Agent 展現深度推理能力，幾乎每次行動前都有充分思考。Prompt 設計優異。",
            "good": "大部分行動有推理支撐。可考慮在 system prompt 中強調「think step by step」。",
            "fair": "推理密度中等。建議：在 system prompt 中加入「分析問題後再執行」的明確指引。",
            "needs_improvement": "Agent 傾向直接執行而非先思考。建議：(1) 加入 chain-of-thought 引導 (2) 對複雜任務要求先輸出計畫 (3) 增加 reasoning token budget。",
            "poor": "幾乎無推理過程，Agent 在盲目試錯。需要：(1) 在 system prompt 強制要求推理 (2) 使用 structured output 要求先輸出分析再輸出行動 (3) 考慮使用支援 extended thinking 的模型。",
        },
    },
    "CONV": {
        "name": "收斂力",
        "en": "Convergence",
        "icon": "🎯",
        "color": "#fff176",
        "description": (
            "評估整個 Session 是否成功朝目標收斂。"
            "包括任務完成率、是否中途 abort、是否需要頻繁 context compaction 等。"
            "高收斂力代表 Session 的 Prompt 策略有效引導模型完成任務。"
        ),
        "what_measured": [
            "session 是否以 task_complete 事件結束",
            "abort 和 turn_aborted 事件的數量",
            "context compaction 的頻率（間接指標）",
            "長 session（>20 turns）的懲罰調整",
        ],
        "suggestions": {
            "excellent": "Session 高效收斂，任務順利完成。整體 Prompt 策略極佳。",
            "good": "任務基本完成，少量 compaction 但不影響結果。",
            "fair": "收斂過程中有一些波折。建議：檢查是否有不必要的長 log 導致頻繁 compaction。",
            "needs_improvement": "收斂力偏低。建議：(1) 在任務開始時設定清晰的完成標準 (2) 減少不必要的 context compaction (3) 避免在單一 session 中處理過多不相關任務。",
            "poor": "Session 未能收斂，可能有 abort 或嚴重偏離目標。需要：(1) 將大任務拆分為多個 session (2) 在 system prompt 中加入 milestone 檢查點 (3) 實作 auto-recovery 機制。",
        },
    },
    "TOOL": {
        "name": "工具效率",
        "en": "Tool Efficiency",
        "icon": "🔧",
        "color": "#26c6da",
        "description": (
            "衡量 Agent 對工具的使用是否高效且有成效。"
            "包括工具呼叫的成功率、是否有冗餘重複的呼叫、"
            "以及工具輸出是否被後續回應有效利用。"
            "高效的工具使用代表 Prompt 成功引導模型選擇正確的工具並善用結果。"
        ),
        "what_measured": [
            "工具呼叫成功率（success / total ratio）",
            "冗餘呼叫偵測（連續相同 tool + 相同 arguments）",
            "工具輸出利用率（output 是否被後續回應引用）",
            "失敗後的恢復策略（是否調整參數重試）",
        ],
        "suggestions": {
            "excellent": "工具使用精準高效，幾乎所有呼叫都成功且輸出被有效利用。維持現有策略。",
            "good": "工具使用效率良好，少量失敗但能自主修正。可微調 tool selection 邏輯。",
            "fair": "部分工具呼叫冗餘或失敗。建議：檢查是否有重複的 tool call，減少不必要的嘗試。",
            "needs_improvement": "工具使用效率偏低。建議：(1) 減少盲目的 trial-and-error (2) 在 tool call 前先確認參數正確性 (3) 避免對同一 tool 重複相同 arguments。",
            "poor": "工具使用混亂，大量失敗或冗餘呼叫。需要：(1) 在 system prompt 中加入 tool usage 指引 (2) 限制同一 tool 的最大連續呼叫次數 (3) 實作 tool call validation 機制。",
        },
    },
}


def _grade_key(score: float) -> str:
    if score >= 90:
        return "excellent"
    elif score >= 80:
        return "good"
    elif score >= 70:
        return "fair"
    elif score >= 60:
        return "needs_improvement"
    else:
        return "poor"


def _grade_label(score: float) -> str:
    labels = {
        "excellent": "優秀 (A)",
        "good": "良好 (B)",
        "fair": "尚可 (C)",
        "needs_improvement": "待改善 (D)",
        "poor": "不及格 (F)",
    }
    return labels[_grade_key(score)]


def _score_css_color(score: float) -> str:
    if score >= 80:
        return "#4caf50"
    elif score >= 60:
        return "#ff9800"
    else:
        return "#f44336"


def _render_diagnosis_summary_html(summary: DiagnosisSummary | None) -> str:
    """Render weighted diagnosis summary section."""

    if summary is None:
        return ""

    pm_guide = "".join(
        "<li><strong>{field}</strong>: {meaning}</li>".format(
            field=html.escape(str(item.get("field", "PM"))),
            meaning=html.escape(str(item.get("meaning_zh", ""))),
        )
        for item in summary.pm_field_guide
    ) or "<li>無</li>"

    pm_items = "".join(
        """
        <li>
            <strong>{field}</strong> — {label}<br>
            <span class="text-dim">{meaning}</span><br>
            <span class="text-dim">Fx 加權比重：{fx}</span>
        </li>
        """.format(
            field=html.escape(str(item.get("field", "PM"))),
            label=html.escape(str(item.get("label_zh", "無"))),
            meaning=html.escape(str(item.get("field_meaning_zh", "無"))),
            fx=html.escape(str(item.get("fx_weight_ratio_zh", "無"))),
        )
        for item in summary.pm_candidates
    ) or "<li>無</li>"

    fx_items = "".join(
        "<li><strong>{fx}</strong> — {label}: {weight}</li>".format(
            fx=html.escape(str(item.get("fx", ""))),
            label=html.escape(str(item.get("family_label_zh", ""))),
            weight=html.escape(str(item.get("weight_pct", "0.0%"))),
        )
        for item in summary.fx_weights
        if float(item.get("weight", 0.0)) > 0
    ) or "<li>無顯著 Fx 權重</li>"

    weighted_dims = "".join(
        "<li><strong>{dim}</strong> — raw={raw} / attention={attention} / Fx={fx}</li>".format(
            dim=html.escape(str(item.get("dimension_zh", item.get("dimension", "")))),
            raw=html.escape(str(item.get("raw_score", ""))),
            attention=html.escape(str(item.get("combined_attention_pct", ""))),
            fx=html.escape(str(item.get("fx_weight_ratio_zh", "無"))),
        )
        for item in summary.weighted_dimensions[:5]
    ) or "<li>無</li>"

    if summary.scope == "batch":
        route_items = "".join(
            "<li><strong>{label}</strong>：{count}</li>".format(
                label=html.escape(str(item.get("label_zh", "未解析"))),
                count=html.escape(str(item.get("count", 0))),
            )
            for item in summary.route_summary.get("top_primary_families", [])
        ) or "<li>無</li>"
    else:
        route_items = """
            <li><strong>主家族</strong>: {primary}</li>
            <li><strong>次家族</strong>: {secondary}</li>
            <li><strong>破損不變量</strong>: {invariant}</li>
            <li><strong>優先修復方向</strong>: {fix}</li>
            <li><strong>誤修風險</strong>: {risk}</li>
            <li><strong>信心 / 證據</strong>: {confidence} / {evidence}</li>
        """.format(
            primary=html.escape(str(summary.route_summary.get("primary_family_zh", "未解析"))),
            secondary=html.escape(str(summary.route_summary.get("secondary_family_zh", "無"))),
            invariant=html.escape(str(summary.route_summary.get("broken_invariant_zh", "尚未判定"))),
            fix=html.escape(str(summary.route_summary.get("first_fix_zh", "無"))),
            risk=html.escape(str(summary.route_summary.get("misrepair_risk_zh", "無"))),
            confidence=html.escape(str(summary.route_summary.get("confidence_zh", "低"))),
            evidence=html.escape(str(summary.route_summary.get("evidence_sufficiency_zh", "薄弱"))),
        )

    signal_items = "".join(
        "<li>{label}</li>".format(label=html.escape(str(item.get("signal_zh", item.get("signal", "")))))
        for item in summary.supporting_evidence.get("failure_signals", [])[:5]
    ) or "<li>無</li>"

    failed_tools = "".join(
        f"<li>{html.escape(str(item))}</li>"
        for item in summary.supporting_evidence.get("failed_tools", [])[:5]
    ) or "<li>無</li>"

    representative_turns = "".join(
        "<li>Turn {turn} — composite {composite}</li>".format(
            turn=html.escape(str(item.get("turn", "?"))),
            composite=html.escape(str(item.get("composite", "?"))),
        )
        for item in summary.supporting_evidence.get("representative_turns", [])[:5]
    ) or "<li>無</li>"

    notes = "".join(
        f"<li>{html.escape(str(note))}</li>"
        for note in summary.notes
    ) or "<li>無</li>"

    return f"""
<div class="agent-analysis">
    <h2>🧭 加權診斷摘要</h2>
    <div class="agent-content">
        <h3>摘要</h3>
        <p>{html.escape(summary.summary_zh or '無')}</p>
        <h3>PM 欄位說明</h3>
        <ul>{pm_guide}</ul>
        <h3>ProblemMap 候選</h3>
        <ul>{pm_items}</ul>
        <h3>Fx 加權比重</h3>
        <ul>{fx_items}</ul>
        <h3>加權後重點維度</h3>
        <ul>{weighted_dims}</ul>
        <h3>{'批次主家族分布' if summary.scope == 'batch' else '路由摘要'}</h3>
        <ul>{route_items}</ul>
        <h3>Supporting Evidence</h3>
        <h4>Failure Signals</h4>
        <ul>{signal_items}</ul>
        <h4>Failed Tools</h4>
        <ul>{failed_tools}</ul>
        <h4>Representative Turns</h4>
        <ul>{representative_turns}</ul>
        <h3>備註</h3>
        <ul>{notes}</ul>
    </div>
</div>
"""


def _render_problemmap_html(diagnosis: ProblemMapDiagnosis | None) -> str:
    """Render ProblemMap / Atlas diagnosis section."""

    if diagnosis is None:
        return ""

    atlas = diagnosis.atlas
    pm1_items = "".join(
        "<li><strong>{number}</strong> — {label} ({confidence})</li>".format(
            number=html.escape(str(item.get("number", "?"))),
            label=html.escape(str(item.get("label_zh", item.get("label", "unknown")))),
            confidence=html.escape(str(item.get("confidence_zh", item.get("confidence", "unknown")))),
        )
        for item in diagnosis.pm1_candidates
    ) or "<li>無明確 PM1 候選</li>"

    references = "".join(
        f"<li>{html.escape(ref)}</li>" for ref in diagnosis.references_used
    ) or "<li>無</li>"

    return f"""
<div class="agent-analysis">
    <h2>🧭 ProblemMap / Atlas 診斷</h2>
    <div class="agent-content">
        <h3>PM1 候選</h3>
        <ul>{pm1_items}</ul>
        <h3>Atlas 家族</h3>
        <ul>
            <li><strong>主家族</strong>: {html.escape(str(atlas.get('primary_family_zh', atlas.get('primary_family', '未解析'))))}</li>
            <li><strong>次家族</strong>: {html.escape(str(atlas.get('secondary_family_zh', atlas.get('secondary_family', '無'))))}</li>
            <li><strong>破損不變量</strong>: {html.escape(str(atlas.get('broken_invariant_zh', atlas.get('broken_invariant', '尚未判定'))))}</li>
            <li><strong>為何主而非次</strong>: {html.escape(str(atlas.get('why_primary_not_secondary_zh', atlas.get('why_primary_not_secondary', '無'))))}</li>
            <li><strong>優先修復方向</strong>: {html.escape(str(atlas.get('fix_surface_direction_zh', atlas.get('fix_surface_direction', '無'))))}</li>
            <li><strong>誤修風險</strong>: {html.escape(str(atlas.get('misrepair_risk_zh', atlas.get('misrepair_risk', '無'))))}</li>
            <li><strong>信心</strong>: {html.escape(str(atlas.get('confidence_zh', atlas.get('confidence', '低'))))}</li>
            <li><strong>證據充分度</strong>: {html.escape(str(atlas.get('evidence_sufficiency_zh', atlas.get('evidence_sufficiency', '薄弱'))))}</li>
        </ul>
        <h3>全域修復路徑</h3>
        <ul>
            <li><strong>對應家族</strong>: {html.escape(str(diagnosis.global_fix_route.get('family_zh', diagnosis.global_fix_route.get('family', '無')) or '無'))}</li>
            <li><strong>建議頁面</strong>: {html.escape(str(diagnosis.global_fix_route.get('page_zh', diagnosis.global_fix_route.get('page', '無')) or '無'))}</li>
            <li><strong>最小修復動作</strong>: {html.escape(str(diagnosis.global_fix_route.get('minimal_fix_zh', diagnosis.global_fix_route.get('minimal_fix', '無')) or '無'))}</li>
        </ul>
        <h3>參考來源</h3>
        <ul>{references}</ul>
    </div>
</div>
"""


def _render_evidence_summary_html(evidence_summary: Dict[str, object]) -> str:
    """Render normalized evidence summary section."""

    if not evidence_summary:
        return ""

    representative_turns = evidence_summary.get("representative_turns", [])
    turn_items = "".join(
        "<li>Turn {turn} — composite {composite} / tools {tools}</li>".format(
            turn=html.escape(str(item.get("turn", "?"))),
            composite=html.escape(str(item.get("composite", "?"))),
            tools=html.escape(", ".join(item.get("tools", [])) or "none"),
        )
        for item in representative_turns[:5]
    ) or "<li>無</li>"

    failed_tools = "".join(
        f"<li>{html.escape(str(name))}</li>"
        for name in evidence_summary.get("failed_tools", [])[:10]
    ) or "<li>無</li>"

    weak_dimensions = "".join(
        "<li>{name}: {value}</li>".format(
            name=html.escape(str(name)),
            value=html.escape(f"{value:.1f}" if isinstance(value, (int, float)) else str(value)),
        )
        for name, value in evidence_summary.get("weak_dimensions", {}).items()
    ) or "<li>無</li>"

    signals = "".join(
        f"<li>{html.escape(str(signal))}</li>"
        for signal in evidence_summary.get("candidate_failure_signals", [])[:10]
    ) or "<li>無</li>"

    return f"""
<div class="agent-analysis">
    <h2>🧪 Evidence Summary</h2>
    <div class="agent-content">
        <h3>Weak Dimensions</h3>
        <ul>{weak_dimensions}</ul>
        <h3>Candidate Failure Signals</h3>
        <ul>{signals}</ul>
        <h3>Representative Turns</h3>
        <ul>{turn_items}</ul>
        <h3>Failed Tools</h3>
        <ul>{failed_tools}</ul>
    </div>
</div>
"""


def _render_artifact_sources_html(sources: Dict[str, str]) -> str:
    """Render artifact source metadata."""

    if not sources:
        return ""

    items = "".join(
        "<li><strong>{key}</strong>: {value}</li>".format(
            key=html.escape(str(key)),
            value=html.escape(str(value)),
        )
        for key, value in sorted(sources.items())
    )
    return f"""
<div class="agent-analysis">
    <h2>🗃️ Artifact Sources</h2>
    <div class="agent-content">
        <ul>{items}</ul>
    </div>
</div>
"""


def _render_batch_html(batch: BatchReport) -> str:
    """Generate a standalone HTML page for a batch report."""

    rows = []
    for report in batch.sessions:
        primary = "未解析"
        route = ""
        if report.problemmap is not None:
            primary = str(report.problemmap.atlas.get("primary_family_zh", report.problemmap.atlas.get("primary_family", "未解析")))
            route = report.problemmap.global_fix_route.get("minimal_fix_zh") or report.problemmap.global_fix_route.get("minimal_fix") or json.dumps(
                report.problemmap.global_fix_route, ensure_ascii=False
            )
        rows.append(
            """
            <tr>
                <td>{session_id}</td>
                <td>{score}</td>
                <td>{grade}</td>
                <td>{primary}</td>
                <td>{route}</td>
            </tr>
            """.format(
                session_id=html.escape(report.score.session_id or "unknown"),
                score=html.escape(f"{report.score.composite:.1f}"),
                grade=html.escape(report.score.grade),
                primary=html.escape(primary),
                route=html.escape(route),
            )
        )

    evidence_json = html.escape(
        json.dumps(batch.evidence_summary, indent=2, ensure_ascii=False)
    )
    layers = ", ".join(batch.analysis_layers)
    agent_section = ""
    if batch.agent_analysis is not None:
        agent_section = render_agent_html_section(batch.agent_analysis)
    diagnosis_section = _render_diagnosis_summary_html(batch.diagnosis_summary)
    return f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session Health Batch Report</title>
<style>
body {{
    font-family: 'Segoe UI', 'Noto Sans TC', -apple-system, sans-serif;
    background: #1a1a2e;
    color: #e8e8e8;
    line-height: 1.6;
    padding: 2rem;
    max-width: 1200px;
    margin: 0 auto;
}}
.card {{
    background: #16213e;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
}}
h1, h2 {{ color: #4fc3f7; }}
table {{
    width: 100%;
    border-collapse: collapse;
}}
th, td {{
    border-bottom: 1px solid #2a2a4a;
    padding: 0.75rem;
    text-align: left;
    vertical-align: top;
}}
th {{ color: #4fc3f7; }}
pre {{
    white-space: pre-wrap;
    word-break: break-word;
    background: #0f3460;
    border-radius: 8px;
    padding: 1rem;
}}
.agent-analysis {{
    background: #16213e;
    border: 1px solid #2a2a4a;
    border-radius: 12px;
    padding: 1.5rem;
    margin-bottom: 1.5rem;
}}
.agent-analysis h2 {{ color: #4fc3f7; }}
.agent-meta {{
    color: #8892a4;
    font-size: 0.9rem;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid #2a2a4a;
}}
.agent-content h3 {{ color: #4fc3f7; margin: 0.8rem 0 0.3rem; }}
.agent-content h4 {{ margin: 0.6rem 0 0.2rem; }}
.agent-content p, .agent-content li {{ margin-bottom: 0.3rem; }}
.agent-content ul, .agent-content ol {{ padding-left: 1.4rem; }}
.text-dim {{ color: #8892a4; }}
</style>
</head>
<body>
<div class="card">
    <h1>⚔ Session Health Batch Report</h1>
    <p>Sessions: {len(batch.sessions)} ｜ Target: {html.escape(batch.target_kind)} ｜ Layers: {html.escape(layers)}</p>
</div>
<div class="card">
    <h2>📋 Batch Summary</h2>
    <table>
        <thead>
            <tr>
                <th>Session</th>
                <th>Score</th>
                <th>Grade</th>
                <th>主家族</th>
                <th>最小修復動作</th>
            </tr>
        </thead>
        <tbody>
            {''.join(rows)}
        </tbody>
    </table>
</div>
<div class="card">
    <h2>🧪 Batch Evidence</h2>
    <pre>{evidence_json}</pre>
</div>
{diagnosis_section}
{agent_section}
</body>
</html>
"""


def render_html(item: SessionScore | SessionReport | BatchReport, agent_section: str = "") -> str:
    """Generate a complete standalone HTML report."""
    if isinstance(item, BatchReport):
        return _render_batch_html(item)

    score = item.score if isinstance(item, SessionReport) else item
    axes = score.radar_axes

    # Build radar chart SVG data
    radar_svg = _build_radar_svg(axes)

    # Build dimension cards HTML
    dim_cards = _build_dim_cards(axes)

    # Session info
    sid = html.escape(score.session_id or "unknown")
    source = html.escape(score.source)
    model = html.escape(score.model or "unknown")
    comp_color = _score_css_color(score.composite)

    extra_sections = ""
    if isinstance(item, SessionReport):
        extra_sections = "".join(
            [
                _render_diagnosis_summary_html(item.diagnosis_summary),
                _render_artifact_sources_html(item.artifact_sources),
                render_agent_html_section(item.agent_analysis)
                if item.agent_analysis is not None
                else "",
            ]
        )
    elif agent_section:
        extra_sections = agent_section

    return _HTML_TEMPLATE.format(
        session_id=sid,
        source=source,
        model=model,
        turn_count=score.turn_count,
        composite=f"{score.composite:.1f}",
        composite_color=comp_color,
        grade=score.grade,
        grade_label=_grade_label(score.composite),
        radar_svg=radar_svg,
        dim_cards=dim_cards,
        agent_section=extra_sections,
        compaction_count=score.compaction_count,
        abort_count=score.abort_count,
        composite_min=f"{score.composite_min:.1f}",
        composite_max=f"{score.composite_max:.1f}",
        composite_stddev=f"{score.composite_stddev:.1f}",
        axes_json=json.dumps({k: round(v, 1) for k, v in axes.items()}, ensure_ascii=False),
    )


def _build_radar_svg(axes: Dict[str, float]) -> str:
    """Build SVG radar chart (supports N axes)."""
    cx, cy = 200, 200  # center
    r = 160  # max radius
    order = DIM_ORDER
    n = len(order)
    angle_step = 360.0 / n

    # Grid lines (20%, 40%, 60%, 80%, 100%)
    grid_lines = []
    for pct in [0.2, 0.4, 0.6, 0.8, 1.0]:
        pts = []
        for i in range(n):
            angle = math.radians(-90 + i * angle_step)
            x = cx + r * pct * math.cos(angle)
            y = cy + r * pct * math.sin(angle)
            pts.append(f"{x:.1f},{y:.1f}")
        grid_lines.append(f'<polygon points="{" ".join(pts)}" class="grid-line"/>')

    # Axis lines
    axis_lines = []
    for i in range(n):
        angle = math.radians(-90 + i * angle_step)
        x = cx + r * math.cos(angle)
        y = cy + r * math.sin(angle)
        axis_lines.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" class="axis-line"/>')

    # Data polygon
    data_pts = []
    for i, name in enumerate(order):
        val = axes.get(name, 0) / 100.0
        angle = math.radians(-90 + i * angle_step)
        x = cx + r * val * math.cos(angle)
        y = cy + r * val * math.sin(angle)
        data_pts.append(f"{x:.1f},{y:.1f}")
    data_polygon = f'<polygon points="{" ".join(data_pts)}" class="data-area"/>'

    # Data points (dots)
    data_dots = []
    for i, name in enumerate(order):
        val = axes.get(name, 0) / 100.0
        angle = math.radians(-90 + i * angle_step)
        x = cx + r * val * math.cos(angle)
        y = cy + r * val * math.sin(angle)
        color = DIM_META[name]["color"]
        data_dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="{color}" stroke="#fff" stroke-width="2"/>')

    # Labels
    labels = []
    for i, name in enumerate(order):
        meta = DIM_META[name]
        angle = math.radians(-90 + i * angle_step)
        lx = cx + (r + 30) * math.cos(angle)
        ly = cy + (r + 30) * math.sin(angle)
        anchor = "middle"
        angle_deg = -90 + i * angle_step
        # Normalize to 0-360
        norm = angle_deg % 360
        if 45 < norm < 135:
            anchor = "start"
        elif 225 < norm < 315:
            anchor = "end"
        val = axes.get(name, 0)
        labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" class="radar-label">'
            f'{meta["icon"]} {name}'
            f'</text>'
            f'<text x="{lx:.1f}" y="{ly + 18:.1f}" text-anchor="{anchor}" class="radar-value">'
            f'{val:.0f}'
            f'</text>'
        )

    return "\n".join(grid_lines + axis_lines + [data_polygon] + data_dots + labels)


def _build_dim_cards(axes: Dict[str, float]) -> str:
    """Build HTML cards for each dimension."""
    cards = []
    for name in DIM_ORDER:
        val = axes.get(name, 0)
        meta = DIM_META[name]
        grade = _grade_key(val)
        suggestion = meta["suggestions"][grade]
        color = _score_css_color(val)
        bar_pct = max(2, val)

        measured_items = "\n".join(
            f"<li>{html.escape(item)}</li>" for item in meta["what_measured"]
        )

        cards.append(f"""
        <div class="dim-card">
            <div class="dim-header">
                <div class="dim-title">
                    <span class="dim-icon">{meta['icon']}</span>
                    <span class="dim-name">{name} — {meta['name']}</span>
                    <span class="dim-en">{meta['en']}</span>
                </div>
                <div class="dim-score" style="color: {color}">
                    {val:.1f}
                    <span class="dim-grade">{_grade_label(val)}</span>
                </div>
            </div>
            <div class="dim-bar-track">
                <div class="dim-bar-fill" style="width: {bar_pct:.1f}%; background: {color}"></div>
            </div>
            <div class="dim-body">
                <div class="dim-section">
                    <h4>📋 說明</h4>
                    <p>{html.escape(meta['description'])}</p>
                </div>
                <div class="dim-section">
                    <h4>📏 量測項目</h4>
                    <ul>{measured_items}</ul>
                </div>
                <div class="dim-section suggestion">
                    <h4>💡 改善建議</h4>
                    <p>{html.escape(suggestion)}</p>
                </div>
            </div>
        </div>
        """)
    return "\n".join(cards)


# ── HTML Template ──

_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Session Health Report — {session_id}</title>
<style>
:root {{
    --bg: #1a1a2e;
    --card: #16213e;
    --card-alt: #0f3460;
    --text: #e8e8e8;
    --text-dim: #8892a4;
    --accent: #4fc3f7;
    --border: #2a2a4a;
    --green: #4caf50;
    --yellow: #ff9800;
    --red: #f44336;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    font-family: 'Segoe UI', 'Noto Sans TC', -apple-system, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
    padding: 2rem;
    max-width: 960px;
    margin: 0 auto;
}}
.report-header {{
    text-align: center;
    margin-bottom: 2rem;
    padding: 2rem;
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
}}
.report-header h1 {{
    font-size: 1.6rem;
    margin-bottom: 0.5rem;
    color: var(--accent);
}}
.report-header .meta {{
    color: var(--text-dim);
    font-size: 0.9rem;
}}
.report-header .meta span {{
    margin: 0 0.8rem;
}}
.overall-score {{
    text-align: center;
    padding: 1.5rem 2rem;
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
    margin-bottom: 2rem;
}}
.score-big {{
    font-size: 3.5rem;
    font-weight: bold;
    line-height: 1;
}}
.score-label {{
    font-size: 1rem;
    color: var(--text-dim);
    margin-top: 0.5rem;
}}
.score-bar {{
    height: 8px;
    background: var(--border);
    border-radius: 4px;
    margin-top: 1rem;
    overflow: hidden;
}}
.score-bar-fill {{
    height: 100%;
    border-radius: 4px;
    transition: width 0.8s ease;
}}
.radar-section {{
    text-align: center;
    padding: 1.5rem;
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
    margin-bottom: 2rem;
}}
.radar-section h2 {{
    font-size: 1.2rem;
    color: var(--accent);
    margin-bottom: 1rem;
}}
svg.radar {{
    max-width: 100%;
    height: auto;
}}
.grid-line {{
    fill: none;
    stroke: var(--border);
    stroke-width: 1;
}}
.axis-line {{
    stroke: var(--border);
    stroke-width: 1;
    stroke-dasharray: 4 4;
}}
.data-area {{
    fill: rgba(79, 195, 247, 0.15);
    stroke: var(--accent);
    stroke-width: 2.5;
}}
.radar-label {{
    fill: var(--text);
    font-size: 14px;
    font-weight: bold;
}}
.radar-value {{
    fill: var(--text-dim);
    font-size: 13px;
}}
.dim-cards {{
    display: flex;
    flex-direction: column;
    gap: 1rem;
}}
.dim-card {{
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
    overflow: hidden;
}}
.dim-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.5rem;
    background: var(--card-alt);
}}
.dim-title {{
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    flex-wrap: wrap;
}}
.dim-icon {{ font-size: 1.3rem; }}
.dim-name {{
    font-size: 1.1rem;
    font-weight: bold;
}}
.dim-en {{
    font-size: 0.85rem;
    color: var(--text-dim);
}}
.dim-score {{
    font-size: 1.8rem;
    font-weight: bold;
    text-align: right;
    min-width: 80px;
}}
.dim-grade {{
    display: block;
    font-size: 0.75rem;
    font-weight: normal;
    color: var(--text-dim);
}}
.dim-bar-track {{
    height: 6px;
    background: var(--border);
}}
.dim-bar-fill {{
    height: 100%;
    transition: width 0.8s ease;
}}
.dim-body {{
    padding: 1rem 1.5rem;
}}
.dim-section {{
    margin-bottom: 1rem;
}}
.dim-section:last-child {{ margin-bottom: 0; }}
.dim-section h4 {{
    font-size: 0.9rem;
    color: var(--accent);
    margin-bottom: 0.3rem;
}}
.dim-section p {{
    font-size: 0.9rem;
    color: var(--text-dim);
}}
.dim-section ul {{
    padding-left: 1.2rem;
    font-size: 0.9rem;
    color: var(--text-dim);
}}
.dim-section ul li {{ margin-bottom: 0.2rem; }}
.dim-section.suggestion p {{
    color: var(--text);
    background: rgba(79, 195, 247, 0.08);
    padding: 0.8rem 1rem;
    border-radius: 8px;
    border-left: 3px solid var(--accent);
}}
.stats-footer {{
    text-align: center;
    padding: 1rem;
    color: var(--text-dim);
    font-size: 0.85rem;
    margin-top: 1rem;
}}
.stats-footer span {{
    margin: 0 1rem;
}}
.agent-analysis {{
    background: var(--card);
    border-radius: 12px;
    border: 1px solid var(--border);
    padding: 1.5rem;
    margin-top: 2rem;
}}
.agent-analysis h2 {{
    color: var(--accent);
    font-size: 1.2rem;
    margin-bottom: 0.5rem;
}}
.agent-meta {{
    color: var(--text-dim);
    font-size: 0.85rem;
    margin-bottom: 1rem;
    padding-bottom: 0.5rem;
    border-bottom: 1px solid var(--border);
}}
.agent-content {{
    color: var(--text);
    font-size: 0.95rem;
    line-height: 1.7;
}}
.agent-content h3 {{
    color: var(--accent);
    margin-top: 1rem;
    margin-bottom: 0.3rem;
}}
.agent-content h4 {{
    color: var(--text);
    margin-top: 0.8rem;
    margin-bottom: 0.2rem;
}}
.agent-content ul, .agent-content ol {{
    padding-left: 1.5rem;
    margin-bottom: 0.5rem;
}}
.agent-content li {{
    margin-bottom: 0.3rem;
}}
.agent-content p {{
    margin-bottom: 0.5rem;
}}
</style>
</head>
<body>

<div class="report-header">
    <h1>⚔ Session Health Report</h1>
    <div class="meta">
        <span>📎 {session_id}</span><br>
        <span>Source: {source}</span>
        <span>Model: {model}</span>
        <span>Turns: {turn_count}</span>
    </div>
</div>

<div class="overall-score">
    <div class="score-big" style="color: {composite_color}">{composite}</div>
    <div class="score-label">/100 — {grade_label}</div>
    <div class="score-bar">
        <div class="score-bar-fill" style="width: {composite}%; background: {composite_color}"></div>
    </div>
</div>

<div class="radar-section">
    <h2>🕸️ 能力雷達圖</h2>
    <svg class="radar" viewBox="0 0 400 400" xmlns="http://www.w3.org/2000/svg">
{radar_svg}
    </svg>
</div>

<div class="dim-cards">
    <h2 style="color: var(--accent); margin-bottom: 0.5rem;">📊 各維度詳細分析</h2>
{dim_cards}
</div>

{agent_section}

<div class="stats-footer">
    <span>σ = {composite_stddev}</span>
    <span>Min = {composite_min}</span>
    <span>Max = {composite_max}</span>
    <span>Compactions: {compaction_count}</span>
    <span>Aborts: {abort_count}</span>
</div>

</body>
</html>
"""
