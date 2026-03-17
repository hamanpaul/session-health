"""ProblemMap / Atlas diagnosis layer for session-health.

This module adapts the route-first ideas from WFGY ProblemMap and the
skill-problemmap workflow into a local, zero-dependency session diagnosis
layer. It intentionally uses the existing normalized Session/SessionScore
objects as the primary evidence surface.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List

from .parser_base import Session, Turn
from .report_types import DiagnosisSummary, ProblemMapDiagnosis
from .scorer import SessionScore

PM1_HEURISTICS = [
    (1, "hallucination & chunk drift", ["hallucination", "chunk drift", "anchor mismatch", "wrong source", "referent drift"]),
    (2, "interpretation collapse", ["misunderstood", "misread", "interpretation", "instruction collapse"]),
    (3, "long reasoning chains", ["long reasoning", "chain of thought", "recursive chain", "reasoning chain"]),
    (4, "bluffing / overconfidence", ["overconfident", "confident but wrong", "bluff", "fake certainty"]),
    (5, "semantic != embedding", ["embedding", "semantic mismatch", "retrieval mismatch", "vector drift"]),
    (6, "logic collapse & recovery", ["logic collapse", "contradiction", "inference failure", "recovery scaffold"]),
    (7, "memory breaks across sessions", ["memory", "forgot", "context_compacted", "lost context", "session drift", "persistence"]),
    (8, "debugging black box", ["black box", "no trace", "missing logs", "uninspectable", "no visibility"]),
    (9, "entropy collapse", ["entropy collapse", "fragmentation", "drift", "incoherent"]),
    (10, "creative freeze", ["creative freeze", "stuck", "blank", "freeze"]),
    (11, "symbolic collapse", ["symbolic collapse", "json", "schema", "representation", "carrier distortion"]),
    (12, "philosophical recursion", ["philosophical recursion", "self-reference", "meta loop", "recursion"]),
    (13, "multi-agent chaos", ["multi-agent", "parallel", "race", "conflict", "coordinator"]),
    (14, "bootstrap ordering", ["bootstrap", "startup", "init order", "ordering"]),
    (15, "deployment deadlock", ["deploy", "deadlock", "rollout blocked"]),
    (16, "pre-deploy collapse", ["pre-deploy", "predeploy", "ci gate", "before deploy"]),
]

PM1_LABELS_ZH = {
    1: "幻覺與 chunk 漂移",
    2: "詮釋崩塌",
    3: "長推理鏈失穩",
    4: "虛張自信 / 過度自信",
    5: "語意與 embedding 不對齊",
    6: "邏輯崩塌與恢復",
    7: "跨 session 記憶斷裂",
    8: "黑盒除錯",
    9: "熵崩塌",
    10: "創造力凍結",
    11: "符號層崩塌",
    12: "哲學式遞迴",
    13: "多 agent 混亂",
    14: "啟動順序失衡",
    15: "部署死鎖",
    16: "部署前崩塌",
}

ATLAS_FAMILIES = [
    {
        "id": "F1",
        "name": "Grounding & Evidence Integrity",
        "name_zh": "錨定與證據完整性",
        "broken_invariant": "anchor_to_claim_coupling_broken",
        "broken_invariant_zh": "主張與錨點的耦合失效",
        "keywords": [
            "hallucination",
            "anchor mismatch",
            "evidence mismatch",
            "referent drift",
            "wrong source",
            "target mismatch",
            "grounding",
        ],
        "pm1_support": [1, 5],
        "first_fix": "re-grounding -> evidence verification -> target-reference audit",
        "first_fix_zh": "先重新錨定，再做證據核對與目標 / 參照審計",
        "misrepair": "rewriting tone or style before anchor restoration",
        "misrepair_zh": "在修復錨點前就先改語氣或風格，容易造成誤修",
    },
    {
        "id": "F2",
        "name": "Reasoning & Progression Integrity",
        "name_zh": "推理與推進完整性",
        "broken_invariant": "progression_continuity_broken",
        "broken_invariant_zh": "推進鏈的連續性失效",
        "keywords": [
            "reasoning",
            "inference",
            "logic collapse",
            "recursive",
            "decomposition",
            "contradiction",
            "progression break",
            "loop",
        ],
        "pm1_support": [2, 3, 4, 6, 10, 12],
        "first_fix": "decomposition reset -> interpretation checkpoint -> recovery scaffold",
        "first_fix_zh": "先重設拆解，再做詮釋檢查點與恢復支架",
        "misrepair": "redesigning the carrier when the real failure is progression",
        "misrepair_zh": "真正問題在推進鏈時，卻跑去改載體層，容易偏修",
    },
    {
        "id": "F3",
        "name": "State & Continuity Integrity",
        "name_zh": "狀態與連續性完整性",
        "broken_invariant": "state_continuity_broken",
        "broken_invariant_zh": "狀態連續性失效",
        "keywords": [
            "memory",
            "ownership",
            "role",
            "continuity",
            "persistence",
            "session drift",
            "multi-agent",
            "interaction thread",
        ],
        "pm1_support": [7, 13],
        "first_fix": "continuity restoration -> role fencing -> provenance tracing",
        "first_fix_zh": "先恢復連續性，再補角色邊界與來源追蹤",
        "misrepair": "adding more instructions before continuity infrastructure is restored",
        "misrepair_zh": "在連續性基礎設施未恢復前就加更多指令，容易雪上加霜",
    },
    {
        "id": "F4",
        "name": "Execution & Contract Integrity",
        "name_zh": "執行與契約完整性",
        "broken_invariant": "execution_skeleton_closure_broken",
        "broken_invariant_zh": "執行骨架的閉合性失效",
        "keywords": [
            "bootstrap",
            "ordering",
            "readiness",
            "bridge",
            "liveness",
            "deadlock",
            "deployment",
            "contract",
            "protocol",
            "execution",
            "closure",
        ],
        "pm1_support": [14, 15, 16],
        "first_fix": "readiness audit -> ordering validation -> bridge and closure-path trace",
        "first_fix_zh": "先做 readiness 稽核，再驗證順序與 bridge / closure path",
        "misrepair": "improving reasoning before fixing the runtime skeleton",
        "misrepair_zh": "runtime 骨架未修好前就先優化推理，通常修不到根因",
    },
    {
        "id": "F5",
        "name": "Observability & Diagnosability Integrity",
        "name_zh": "可觀測性與可診斷性完整性",
        "broken_invariant": "failure_path_visibility_broken",
        "broken_invariant_zh": "失敗路徑可見性失效",
        "keywords": [
            "traceability",
            "audit",
            "visibility",
            "black box",
            "uninspectable",
            "no logs",
            "observability",
            "diagnosability",
            "warning blindness",
        ],
        "pm1_support": [8],
        "first_fix": "observability insertion -> trace exposure -> audit-route uplift",
        "first_fix_zh": "先插入可觀測點，再暴露 trace 與提升稽核路徑",
        "misrepair": "launching higher-order intervention before exposing the failure path",
        "misrepair_zh": "在失敗路徑還沒被看見前就做高階修補，容易變成盲修",
    },
    {
        "id": "F6",
        "name": "Boundary & Safety Integrity",
        "name_zh": "邊界與安全完整性",
        "broken_invariant": "boundary_integrity_broken",
        "broken_invariant_zh": "邊界完整性失效",
        "keywords": [
            "boundary",
            "safety",
            "erosion",
            "capture",
            "overshoot",
            "drift",
            "fragmentation",
            "unstable boundary",
            "alignment",
            "control path",
        ],
        "pm1_support": [9],
        "first_fix": "alignment guard -> control-path audit -> damping and stabilization",
        "first_fix_zh": "先加對齊防護，再稽核控制路徑與做阻尼穩定",
        "misrepair": "improving observability only while the boundary itself is already drifting",
        "misrepair_zh": "邊界本身已漂移時，只補可觀測性仍不足以止血",
    },
    {
        "id": "F7",
        "name": "Representation & Localization Integrity",
        "name_zh": "表徵與定位完整性",
        "broken_invariant": "representation_container_fidelity_broken",
        "broken_invariant_zh": "表徵容器忠實度失效",
        "keywords": [
            "representation",
            "carrier",
            "descriptor",
            "layout",
            "schema",
            "json",
            "symbolic",
            "structural shell",
            "local anchor",
            "ocr",
        ],
        "pm1_support": [11],
        "first_fix": "descriptor audit -> structural preservation -> local anchor repair",
        "first_fix_zh": "先審 descriptor，再保結構、修本地錨點",
        "misrepair": "repairing reasoning or grounding while the carrier remains untrustworthy",
        "misrepair_zh": "載體仍不可信時就先修推理或 grounding，容易持續偏移",
    },
]

CONFIDENCE_LABELS_ZH = {
    "high": "高",
    "medium": "中",
    "low": "低",
}

EVIDENCE_SUFFICIENCY_LABELS_ZH = {
    "sufficient": "充分",
    "partial": "部分",
    "weak": "薄弱",
}

PM_FIELD_GUIDE = [
    {
        "field": "PM1",
        "meaning_zh": "第一優先的 ProblemMap 候選，代表目前最值得先檢查的問題模式。",
    },
    {
        "field": "PM2",
        "meaning_zh": "第二優先候選，用來檢查主路由旁的競爭解釋是否更合理。",
    },
    {
        "field": "PM3",
        "meaning_zh": "第三優先候選，作為補充弱訊號與誤修風險校正。",
    },
]

DIMENSION_LABELS_ZH = {
    "SNR": "信噪比",
    "STATE": "狀態完整度",
    "CTX": "記憶留存",
    "REACT": "反應指標",
    "DEPTH": "推理深度",
    "CONV": "收斂力",
    "TOOL": "工具效率",
}

FAILURE_SIGNAL_LABELS_ZH = {
    "event:turn_aborted": "回合中止",
    "event:context_compacted": "context 壓縮",
    "nonzero-exit-code": "非零退出碼",
    "event:wrong_approach": "重複錯誤路徑",
    "event:misunderstood_request": "需求理解偏差",
    "event:buggy_code": "產出有 bug 的程式碼",
    "event:excessive_changes": "變更過大",
    "missing-traceability": "缺少可追溯性",
}

FAMILY_DIMENSION_WEIGHTS = {
    "F1": {"SNR": 0.10, "STATE": 0.20, "CTX": 0.15, "REACT": 0.10, "DEPTH": 0.10, "CONV": 0.10, "TOOL": 0.25},
    "F2": {"SNR": 0.05, "STATE": 0.05, "CTX": 0.15, "REACT": 0.25, "DEPTH": 0.25, "CONV": 0.20, "TOOL": 0.05},
    "F3": {"SNR": 0.05, "STATE": 0.25, "CTX": 0.35, "REACT": 0.10, "DEPTH": 0.05, "CONV": 0.10, "TOOL": 0.10},
    "F4": {"SNR": 0.05, "STATE": 0.20, "CTX": 0.05, "REACT": 0.10, "DEPTH": 0.05, "CONV": 0.25, "TOOL": 0.30},
    "F5": {"SNR": 0.05, "STATE": 0.35, "CTX": 0.15, "REACT": 0.05, "DEPTH": 0.05, "CONV": 0.10, "TOOL": 0.25},
    "F6": {"SNR": 0.20, "STATE": 0.20, "CTX": 0.10, "REACT": 0.20, "DEPTH": 0.05, "CONV": 0.15, "TOOL": 0.10},
    "F7": {"SNR": 0.25, "STATE": 0.20, "CTX": 0.15, "REACT": 0.10, "DEPTH": 0.10, "CONV": 0.05, "TOOL": 0.15},
}

FAILURE_SIGNAL_FAMILY_HINTS = {
    "event:context_compacted": ["F3"],
    "event:turn_aborted": ["F4"],
    "event:wrong_approach": ["F2"],
    "event:misunderstood_request": ["F2"],
    "event:buggy_code": ["F4"],
    "event:excessive_changes": ["F2", "F4"],
    "nonzero-exit-code": ["F4"],
    "missing-traceability": ["F5"],
}

REFERENCE_URLS = [
    "https://github.com/onestardao/WFGY/blob/main/ProblemMap/README.md",
    "https://github.com/onestardao/WFGY/blob/main/ProblemMap/Atlas/troubleshooting-atlas-router-v1.txt",
]


def build_evidence_summary(session: Session, score: SessionScore) -> Dict[str, Any]:
    """Summarize failure-bearing evidence from the parsed session."""

    first_user = _first_nonempty(turn.user_input for turn in session.turns)
    last_assistant = _first_nonempty(
        turn.assistant_output for turn in reversed(session.turns)
    )

    tool_counts: Dict[str, int] = {}
    failed_tools: List[str] = []
    error_snippets: List[str] = []
    nonzero_exit_codes: List[int] = []
    schema_signals = 0
    json_signals = 0

    for turn in session.turns:
        for tc in turn.tool_calls:
            tool_counts[tc.name] = tool_counts.get(tc.name, 0) + 1
            output_lower = tc.output.lower()
            if tc.success is False or (tc.exit_code is not None and tc.exit_code != 0):
                detail = tc.name
                cmd = str(tc.arguments.get("command", tc.arguments.get("cmd", ""))).strip()
                if cmd:
                    detail = f"{tc.name}: {cmd[:80]}"
                failed_tools.append(detail)
            if tc.exit_code not in (None, 0):
                nonzero_exit_codes.append(tc.exit_code)
            if tc.output and any(token in output_lower for token in ("error:", "traceback", "exception", "failed", "fatal:")):
                error_snippets.append(tc.output.strip()[:180])
            if any(token in output_lower for token in ("schema", "json", "yaml", "xml")):
                schema_signals += 1
                if "json" in output_lower:
                    json_signals += 1

    repeated_turns = [
        ts.index
        for ts in score.turn_scores
        if ts.reaction_result and ts.reaction_result.repeated_commands > 0
    ]
    low_state_turns = [
        ts.index
        for ts in score.turn_scores
        if ts.state < 70
    ]
    weak_dimensions = {
        name: value
        for name, value in score.radar_axes.items()
        if value < 70
    }

    candidate_failure_signals: List[str] = []
    if score.abort_count > 0:
        candidate_failure_signals.append("event:turn_aborted")
    if score.compaction_count > 0:
        candidate_failure_signals.append("event:context_compacted")
    if failed_tools or nonzero_exit_codes:
        candidate_failure_signals.append("nonzero-exit-code")
    if repeated_turns:
        candidate_failure_signals.append("event:wrong_approach")
    if score.state < 70 or low_state_turns:
        candidate_failure_signals.append("missing-traceability")

    hint_tokens: List[str] = []
    if score.context < 70 or score.compaction_count > 0:
        hint_tokens.extend(["memory", "context_compacted", "lost context", "session drift", "continuity"])
    if score.reaction < 70 or repeated_turns:
        hint_tokens.extend(["logic collapse", "reasoning", "loop", "progression break", "recovery scaffold"])
    if score.state < 70 or score.tool_efficiency < 70:
        hint_tokens.extend(["black box", "no trace", "observability", "traceability", "missing logs"])
    if failed_tools or score.abort_count > 0:
        hint_tokens.extend(["execution", "contract", "bootstrap", "ordering", "closure"])
    if score.snr < 70:
        hint_tokens.extend(["representation", "carrier distortion", "noise"])
    if schema_signals:
        hint_tokens.extend(["schema", "representation"])
    if json_signals:
        hint_tokens.extend(["json", "symbolic collapse"])

    evidence_texts = [
        _clip_text(first_user, 220),
        _clip_text(last_assistant, 220),
        *(_clip_text(item, 160) for item in error_snippets[:5]),
        *failed_tools[:5],
    ]
    evidence_texts = [item for item in evidence_texts if item]

    return {
        "first_user_message": _clip_text(first_user, 220),
        "last_assistant_message": _clip_text(last_assistant, 220),
        "weak_dimensions": weak_dimensions,
        "top_tools": sorted(tool_counts.items(), key=lambda item: (-item[1], item[0]))[:5],
        "failed_tools": failed_tools[:5],
        "nonzero_exit_codes": nonzero_exit_codes[:5],
        "repeated_turns": repeated_turns[:10],
        "low_state_turns": low_state_turns[:10],
        "candidate_failure_signals": candidate_failure_signals,
        "hint_tokens": hint_tokens,
        "evidence_texts": evidence_texts,
        "representative_turns": _pick_representative_turns(score),
    }


def diagnose_problemmap(
    session: Session,
    score: SessionScore,
    diagnostic_mode: str = "strict",
    evidence_summary: Dict[str, Any] | None = None,
) -> ProblemMapDiagnosis:
    """Produce a local PM1 + Atlas diagnosis from session evidence."""

    evidence = evidence_summary or build_evidence_summary(session, score)
    text = _build_diagnosis_text(session, score, evidence)
    signals = list(evidence.get("candidate_failure_signals", []))
    pm1_candidates = _match_pm1(text)
    ranked_families = _score_families(text, signals, pm1_candidates)
    fx_weights = _build_fx_weights(ranked_families)

    primary = ranked_families[0]
    secondary = ranked_families[1] if len(ranked_families) > 1 and ranked_families[1]["score"] > 0 else None
    primary_score = int(primary["score"])
    secondary_score = int(secondary["score"]) if secondary else 0
    confidence, evidence_sufficiency = _calibrate_confidence(
        primary_score,
        secondary_score,
        len(evidence.get("evidence_texts", [])),
        len(signals),
    )

    if primary_score <= 0:
        atlas = {
            "primary_family": "unresolved",
            "primary_family_zh": "未解析",
            "secondary_family": "none",
            "secondary_family_zh": "無",
            "why_primary_not_secondary": (
                "No Atlas family has enough structural evidence yet. Prefer "
                "need_more_evidence over decorative precision."
            ),
            "why_primary_not_secondary_zh": "目前還沒有足夠的結構性證據可穩定歸入某個 Atlas 家族，應先補證據，不要硬做裝飾性判斷。",
            "broken_invariant": "undetermined",
            "broken_invariant_zh": "尚未判定",
            "best_current_fit": "no-fit",
            "fit_level": "coarse",
            "fix_surface_direction": "collect better structural evidence before routing",
            "fix_surface_direction_zh": "先補足更好的結構性證據，再決定路由與修復方向。",
            "misrepair_risk": "forcing a decorative family choice under thin evidence",
            "misrepair_risk_zh": "在證據薄弱時硬選家族，容易造成看似精準、實際失焦的誤修。",
            "confidence": confidence,
            "confidence_zh": _translate_confidence_zh(confidence),
            "evidence_sufficiency": evidence_sufficiency,
            "evidence_sufficiency_zh": _translate_evidence_sufficiency_zh(evidence_sufficiency),
        }
        global_fix_route = {
            "family": None,
            "family_zh": None,
            "page": None,
            "page_zh": None,
            "minimal_fix": None,
            "minimal_fix_zh": None,
        }
        need_more_evidence = True
    else:
        family = primary["family"]
        atlas = {
            "primary_family": _format_family_label(family),
            "primary_family_zh": _format_family_label_zh(family),
            "secondary_family": (
                _format_family_label(secondary["family"])
                if secondary
                else "none"
            ),
            "secondary_family_zh": (
                _format_family_label_zh(secondary["family"])
                if secondary
                else "無"
            ),
            "why_primary_not_secondary": _describe_primary_vs_secondary(primary, secondary),
            "why_primary_not_secondary_zh": _describe_primary_vs_secondary_zh(primary, secondary),
            "broken_invariant": family["broken_invariant"],
            "broken_invariant_zh": family["broken_invariant_zh"],
            "best_current_fit": "family-level",
            "fit_level": "family",
            "fix_surface_direction": family["first_fix"],
            "fix_surface_direction_zh": family["first_fix_zh"],
            "misrepair_risk": family["misrepair"],
            "misrepair_risk_zh": family["misrepair_zh"],
            "confidence": confidence,
            "confidence_zh": _translate_confidence_zh(confidence),
            "evidence_sufficiency": evidence_sufficiency,
            "evidence_sufficiency_zh": _translate_evidence_sufficiency_zh(evidence_sufficiency),
        }
        global_fix_route = _build_global_fix_route(family, confidence)
        need_more_evidence = evidence_sufficiency == "weak"

    return ProblemMapDiagnosis(
        status="ok",
        diagnostic_mode=diagnostic_mode,
        pm1_candidates=pm1_candidates,
        fx_weights=fx_weights,
        atlas=atlas,
        global_fix_route=global_fix_route,
        references_used=REFERENCE_URLS,
        source_case=session.id,
        need_more_evidence=need_more_evidence,
    )


def build_diagnosis_summary(
    session: Session,
    score: SessionScore,
    evidence_summary: Dict[str, Any] | None = None,
    problemmap: ProblemMapDiagnosis | None = None,
) -> DiagnosisSummary:
    """Build a weighted diagnosis layer from quantitative and ProblemMap signals."""

    evidence = evidence_summary or build_evidence_summary(session, score)
    diagnosis = problemmap or diagnose_problemmap(session, score, evidence_summary=evidence)
    pm_candidates = _build_pm_field_entries(diagnosis.pm1_candidates, diagnosis.fx_weights)
    quantitative_summary = _build_quantitative_summary(score, evidence)
    weighted_dimensions = _build_weighted_dimensions(score, diagnosis.fx_weights)
    route_summary = _build_route_summary(diagnosis)
    supporting_evidence = _build_supporting_evidence(evidence)
    notes = [
        "Fx 權重只用於診斷解讀層，不會回寫或覆蓋原始 7 軸分數。",
        "PM1 / PM2 / PM3 的中文欄位說明代表候選排序角色，不是新的 taxonomy。",
    ]

    return DiagnosisSummary(
        status="ok",
        scope="single_session",
        summary_zh=_build_summary_text_zh(
            score=score,
            pm_candidates=pm_candidates,
            fx_weights=diagnosis.fx_weights,
            weighted_dimensions=weighted_dimensions,
            route_summary=route_summary,
        ),
        pm_field_guide=PM_FIELD_GUIDE,
        pm_candidates=pm_candidates,
        fx_weights=diagnosis.fx_weights,
        quantitative_summary=quantitative_summary,
        weighted_dimensions=weighted_dimensions,
        route_summary=route_summary,
        supporting_evidence=supporting_evidence,
        notes=notes,
    )


def build_batch_diagnosis_summary(reports: List[Any]) -> DiagnosisSummary:
    """Aggregate weighted diagnosis across a batch of session reports."""

    summaries = [
        getattr(report, "diagnosis_summary", None)
        for report in reports
        if getattr(report, "diagnosis_summary", None) is not None
    ]
    if not summaries:
        return DiagnosisSummary(status="empty", scope="batch")

    count = len(summaries)
    fx_by_id = {family["id"]: [] for family in ATLAS_FAMILIES}
    for summary in summaries:
        for item in summary.fx_weights:
            fx_by_id[item["fx"]].append(float(item.get("weight", 0.0)))

    aggregated_fx = []
    for family in ATLAS_FAMILIES:
        values = fx_by_id.get(family["id"], [])
        avg_weight = sum(values) / count if count else 0.0
        aggregated_fx.append(
            {
                "fx": family["id"],
                "family_label": _format_family_label(family),
                "family_label_zh": _format_family_label_zh(family),
                "weight": round(avg_weight, 4),
                "weight_pct": f"{avg_weight * 100:.1f}%",
            }
        )
    aggregated_fx.sort(key=lambda item: (-item["weight"], item["fx"]))

    dim_rows = _aggregate_weighted_dimensions(summaries)
    pm_candidates = _aggregate_batch_pm_candidates(summaries, aggregated_fx)
    route_summary = _aggregate_batch_route_summary(reports)
    supporting_evidence = _aggregate_batch_supporting_evidence(summaries)
    quantitative_summary = _aggregate_batch_quantitative_summary(reports, summaries)

    return DiagnosisSummary(
        status="ok",
        scope="batch",
        summary_zh=_build_batch_summary_text_zh(route_summary, dim_rows, aggregated_fx),
        pm_field_guide=PM_FIELD_GUIDE,
        pm_candidates=pm_candidates,
        fx_weights=aggregated_fx,
        quantitative_summary=quantitative_summary,
        weighted_dimensions=dim_rows,
        route_summary=route_summary,
        supporting_evidence=supporting_evidence,
        notes=[
            "Batch 模式的 Fx 權重為跨 session 平均值，用於辨識常見結構性問題，不取代單一 session 診斷。",
        ],
    )


def _pick_representative_turns(score: SessionScore) -> List[Dict[str, Any]]:
    """Pick the highest-signal turns for later rendering."""

    ranked: List[tuple[float, Dict[str, Any]]] = []
    for ts in score.turn_scores:
        severity = 0.0
        if ts.composite < 70:
            severity += 2
        if ts.reaction < 70:
            severity += 2
        if ts.context < 70:
            severity += 1.5
        if ts.state < 70:
            severity += 1
        if ts.tool_efficiency < 70:
            severity += 1
        if ts.reaction_result:
            if ts.reaction_result.is_aborted:
                severity += 3
            if ts.reaction_result.repeated_commands:
                severity += 1.5
        if ts.tool_result and ts.tool_result.failed_calls:
            severity += 1.5
        if severity <= 0:
            continue
        ranked.append(
            (
                severity,
                {
                    "turn": ts.index,
                    "composite": round(ts.composite, 1),
                    "reaction": round(ts.reaction, 1),
                    "context": round(ts.context, 1),
                    "state": round(ts.state, 1),
                    "tool_efficiency": round(ts.tool_efficiency, 1),
                },
            )
        )

    ranked.sort(key=lambda item: (-item[0], item[1]["turn"]))
    return [item[1] for item in ranked[:5]]


def _build_diagnosis_text(session: Session, score: SessionScore, evidence: Dict[str, Any]) -> str:
    weak_dims = " ".join(
        f"{name}={value:.0f}" for name, value in evidence.get("weak_dimensions", {}).items()
    )
    top_tools = " ".join(f"{name}({count})" for name, count in evidence.get("top_tools", []))
    signals = " ".join(evidence.get("candidate_failure_signals", []))
    hints = " ".join(evidence.get("hint_tokens", []))
    snippets = " ".join(evidence.get("evidence_texts", []))
    return " ".join(
        part
        for part in [
            session.id,
            session.source,
            session.model,
            evidence.get("first_user_message", ""),
            evidence.get("last_assistant_message", ""),
            weak_dims,
            top_tools,
            signals,
            hints,
            snippets,
            f"compactions {score.compaction_count}",
            f"aborts {score.abort_count}",
        ]
        if part
    ).lower()


def _match_pm1(text: str) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for number, label, keywords in PM1_HEURISTICS:
        hit_count = sum(1 for keyword in keywords if keyword in text)
        if hit_count:
            confidence = "high" if hit_count >= 2 else "medium"
            matches.append(
                {
                    "number": number,
                    "label": label,
                    "label_zh": PM1_LABELS_ZH.get(number, label),
                    "confidence": confidence,
                    "confidence_zh": _translate_confidence_zh(confidence),
                    "score": hit_count,
                }
            )
    matches.sort(key=lambda item: (-int(item["score"]), int(item["number"])))
    for item in matches:
        item.pop("score", None)
    return matches[:3]


def _score_families(
    text: str,
    signals: List[str],
    pm1_candidates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    scored: List[Dict[str, Any]] = []
    for family in ATLAS_FAMILIES:
        score = 0
        matched: List[str] = []
        for keyword in family["keywords"]:
            if keyword in text:
                score += 2 if " " in keyword else 1
                matched.append(keyword)
        supported_pm1 = []
        for candidate in pm1_candidates:
            if int(candidate["number"]) in family["pm1_support"]:
                score += 2
                supported_pm1.append(int(candidate["number"]))
                matched.append(f"pm1:{candidate['number']}")
        for signal in signals:
            if family["id"] in FAILURE_SIGNAL_FAMILY_HINTS.get(signal, []):
                score += 1
                matched.append(signal)
        scored.append(
            {
                "family": family,
                "score": score,
                "matched_keywords": matched,
                "supported_pm1": supported_pm1,
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored


def _describe_primary_vs_secondary(primary: Dict[str, Any], secondary: Dict[str, Any] | None) -> str:
    primary_family = primary["family"]
    primary_matches = primary["matched_keywords"][:4]
    if secondary is None or secondary["score"] <= 0:
        joined = ", ".join(primary_matches) if primary_matches else "coarse structural evidence"
        return (
            f"{primary_family['id']} is primary because the current evidence points first to "
            f"{primary_family['broken_invariant']} ({joined}), and neighboring family pressure is weak."
        )

    secondary_family = secondary["family"]
    primary_joined = ", ".join(primary_matches) if primary_matches else primary_family["broken_invariant"]
    secondary_joined = (
        ", ".join(secondary["matched_keywords"][:3])
        if secondary["matched_keywords"]
        else secondary_family["broken_invariant"]
    )
    return (
        f"{primary_family['id']} beats {secondary_family['id']} because the earliest decisive signals "
        f"fit {primary_family['broken_invariant']} ({primary_joined}) more directly than the neighboring "
        f"pressure for {secondary_family['broken_invariant']} ({secondary_joined})."
    )


def _describe_primary_vs_secondary_zh(primary: Dict[str, Any], secondary: Dict[str, Any] | None) -> str:
    primary_family = primary["family"]
    primary_matches = "、".join(primary["matched_keywords"][:4]) if primary["matched_keywords"] else "目前的結構性訊號"
    if secondary is None or secondary["score"] <= 0:
        return (
            f"{primary_family['id']} 被判為主家族，因為目前最早且最直接的證據先指向「{primary_family['broken_invariant_zh']}」；"
            f"關聯訊號為：{primary_matches}。相鄰家族的競爭壓力目前較弱。"
        )

    secondary_family = secondary["family"]
    secondary_matches = (
        "、".join(secondary["matched_keywords"][:3])
        if secondary["matched_keywords"]
        else "相鄰家族訊號較弱"
    )
    return (
        f"{primary_family['id']} 勝過 {secondary_family['id']}，因為最早的決定性訊號更直接對應「{primary_family['broken_invariant_zh']}」"
        f"（{primary_matches}），而不是 {secondary_family['id']} 的「{secondary_family['broken_invariant_zh']}」"
        f"（{secondary_matches}）。"
    )


def _calibrate_confidence(
    primary_score: int,
    secondary_score: int,
    evidence_count: int,
    signal_count: int,
) -> tuple[str, str]:
    gap = primary_score - secondary_score
    if primary_score >= 6 and gap >= 2 and (evidence_count >= 2 or signal_count >= 2):
        return "high", "sufficient"
    if primary_score >= 3:
        return "medium", "partial"
    return "low", "weak"


def _build_global_fix_route(primary_family: Dict[str, Any], confidence: str) -> Dict[str, Any]:
    if confidence == "low":
        return {
            "family": None,
            "family_zh": None,
            "page": None,
            "page_zh": None,
            "minimal_fix": None,
            "minimal_fix_zh": None,
        }

    family_id = primary_family["id"]
    if family_id == "F4":
        return {
            "family": "Agents & Orchestration",
            "family_zh": "Agents 與協作編排",
            "page": "choose after runtime evidence review",
            "page_zh": "完成 runtime 證據檢查後再決定對應頁面",
            "minimal_fix": "audit readiness, ordering, and closure path before changing prompts",
            "minimal_fix_zh": "先稽核 readiness、順序與 closure path，再決定是否改 prompt。",
        }
    if family_id == "F5":
        return {
            "family": "Eval / Observability",
            "family_zh": "評估 / 可觀測性",
            "page": "choose after traceability review",
            "page_zh": "先完成 traceability 檢查，再決定對應頁面",
            "minimal_fix": "expose the failure path before deeper intervention",
            "minimal_fix_zh": "先把失敗路徑暴露出來，再做更深層的介入。",
        }
    if family_id == "F3":
        return {
            "family": "Agents & Orchestration",
            "family_zh": "Agents 與協作編排",
            "page": "choose after continuity review",
            "page_zh": "完成連續性檢查後再決定對應頁面",
            "minimal_fix": "restore role, persistence, and interaction continuity first",
            "minimal_fix_zh": "先恢復角色、持久性與互動連續性。",
        }
    return {
        "family": None,
        "family_zh": None,
        "page": None,
        "page_zh": None,
        "minimal_fix": None,
        "minimal_fix_zh": None,
    }


def _build_fx_weights(ranked_families: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    scores_by_id = {
        item["family"]["id"]: max(0, int(item.get("score", 0)))
        for item in ranked_families
    }
    total = sum(scores_by_id.values())
    weights: List[Dict[str, Any]] = []
    for family in ATLAS_FAMILIES:
        raw_score = scores_by_id.get(family["id"], 0)
        weight = (raw_score / total) if total > 0 else 0.0
        matched_keywords = next(
            (item.get("matched_keywords", []) for item in ranked_families if item["family"]["id"] == family["id"]),
            [],
        )
        weights.append(
            {
                "fx": family["id"],
                "family_label": _format_family_label(family),
                "family_label_zh": _format_family_label_zh(family),
                "weight": round(weight, 4),
                "weight_pct": f"{weight * 100:.1f}%",
                "raw_score": raw_score,
                "matched_keywords": matched_keywords[:5],
            }
        )
    weights.sort(key=lambda item: (-item["weight"], item["fx"]))
    return weights


def _build_pm_field_entries(
    pm1_candidates: List[Dict[str, Any]],
    fx_weights: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    fx_lookup = {item["fx"]: item for item in fx_weights}
    entries: List[Dict[str, Any]] = []
    for index, candidate in enumerate(pm1_candidates[:3], start=1):
        guide = PM_FIELD_GUIDE[index - 1]
        linked_families = []
        for family in ATLAS_FAMILIES:
            if int(candidate["number"]) not in family["pm1_support"]:
                continue
            fx_info = fx_lookup.get(family["id"], {})
            linked_families.append(
                {
                    "fx": family["id"],
                    "family_label_zh": _format_family_label_zh(family),
                    "weight": float(fx_info.get("weight", 0.0)),
                    "weight_pct": fx_info.get("weight_pct", "0.0%"),
                }
            )
        linked_families.sort(key=lambda item: (-item["weight"], item["fx"]))
        fx_weight_ratio_zh = "、".join(
            f"{item['fx']} {item['weight_pct']}" for item in linked_families if item["weight"] > 0
        ) or "目前沒有顯著 Fx 權重"
        entries.append(
            {
                "field": guide["field"],
                "field_meaning_zh": guide["meaning_zh"],
                "number": candidate["number"],
                "label": candidate["label"],
                "label_zh": candidate.get("label_zh", candidate["label"]),
                "confidence": candidate["confidence"],
                "confidence_zh": candidate.get("confidence_zh", candidate["confidence"]),
                "fx_links": linked_families,
                "fx_weight_ratio_zh": fx_weight_ratio_zh,
            }
        )
    return entries


def _build_quantitative_summary(score: SessionScore, evidence: Dict[str, Any]) -> Dict[str, Any]:
    weak_dimensions = [
        {
            "dimension": name,
            "dimension_zh": DIMENSION_LABELS_ZH.get(name, name),
            "score": round(value, 1),
        }
        for name, value in sorted(
            evidence.get("weak_dimensions", {}).items(),
            key=lambda item: item[1],
        )
    ]
    signals = [
        {
            "signal": signal,
            "signal_zh": FAILURE_SIGNAL_LABELS_ZH.get(signal, signal),
        }
        for signal in evidence.get("candidate_failure_signals", [])
    ]
    return {
        "composite": round(score.composite, 1),
        "grade": score.grade,
        "weak_dimensions": weak_dimensions,
        "top_tools": evidence.get("top_tools", []),
        "failure_signals": signals,
    }


def _build_weighted_dimensions(
    score: SessionScore,
    fx_weights: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    positive_fx = [item for item in fx_weights if float(item.get("weight", 0.0)) > 0]
    for dimension, raw_score in score.radar_axes.items():
        severity = max(0.0, (100.0 - raw_score) / 100.0)
        contributors = []
        fx_relevance = 0.0
        for fx in positive_fx:
            dimension_share = FAMILY_DIMENSION_WEIGHTS.get(fx["fx"], {}).get(dimension, 0.0)
            if dimension_share <= 0:
                continue
            impact = float(fx["weight"]) * dimension_share
            fx_relevance += impact
            contributors.append(
                {
                    "fx": fx["fx"],
                    "family_label_zh": fx["family_label_zh"],
                    "weight": round(impact, 4),
                    "weight_pct": f"{impact * 100:.1f}%",
                }
            )
        contributors.sort(key=lambda item: (-item["weight"], item["fx"]))
        attention = (severity * 0.7) + (fx_relevance * 0.3)
        items.append(
            {
                "dimension": dimension,
                "dimension_zh": DIMENSION_LABELS_ZH.get(dimension, dimension),
                "raw_score": round(raw_score, 1),
                "severity_pct": f"{severity * 100:.1f}%",
                "fx_relevance_pct": f"{fx_relevance * 100:.1f}%",
                "combined_attention": round(attention, 4),
                "combined_attention_pct": f"{attention * 100:.1f}%",
                "fx_weight_ratio_zh": "、".join(
                    f"{item['fx']} {item['weight_pct']}" for item in contributors[:3]
                ) or "無顯著 Fx 加權",
                "contributors": contributors[:3],
            }
        )
    items.sort(key=lambda item: (-item["combined_attention"], item["dimension"]))
    return items


def _build_route_summary(diagnosis: ProblemMapDiagnosis) -> Dict[str, Any]:
    atlas = diagnosis.atlas
    return {
        "primary_family": atlas.get("primary_family", "unresolved"),
        "primary_family_zh": atlas.get("primary_family_zh", atlas.get("primary_family", "未解析")),
        "secondary_family": atlas.get("secondary_family", "none"),
        "secondary_family_zh": atlas.get("secondary_family_zh", atlas.get("secondary_family", "無")),
        "broken_invariant_zh": atlas.get("broken_invariant_zh", atlas.get("broken_invariant", "尚未判定")),
        "why_primary_not_secondary_zh": atlas.get("why_primary_not_secondary_zh", atlas.get("why_primary_not_secondary", "無")),
        "first_fix_zh": atlas.get("fix_surface_direction_zh", atlas.get("fix_surface_direction", "無")),
        "misrepair_risk_zh": atlas.get("misrepair_risk_zh", atlas.get("misrepair_risk", "無")),
        "confidence_zh": atlas.get("confidence_zh", atlas.get("confidence", "低")),
        "evidence_sufficiency_zh": atlas.get("evidence_sufficiency_zh", atlas.get("evidence_sufficiency", "薄弱")),
        "global_fix_route": diagnosis.global_fix_route,
    }


def _build_supporting_evidence(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "failure_signals": [
            {
                "signal": signal,
                "signal_zh": FAILURE_SIGNAL_LABELS_ZH.get(signal, signal),
            }
            for signal in evidence.get("candidate_failure_signals", [])
        ],
        "failed_tools": evidence.get("failed_tools", []),
        "representative_turns": evidence.get("representative_turns", []),
    }


def _build_summary_text_zh(
    score: SessionScore,
    pm_candidates: List[Dict[str, Any]],
    fx_weights: List[Dict[str, Any]],
    weighted_dimensions: List[Dict[str, Any]],
    route_summary: Dict[str, Any],
) -> str:
    weak_dims = "、".join(
        f"{item['dimension_zh']} {item['raw_score']:.1f}"
        for item in weighted_dimensions[:2]
    ) or "無明顯弱項"
    pm_label = pm_candidates[0]["label_zh"] if pm_candidates else "無明確 ProblemMap 候選"
    top_fx = "、".join(
        f"{item['fx']} {item['weight_pct']}"
        for item in fx_weights[:2]
        if float(item.get("weight", 0.0)) > 0
    ) or "無顯著 Fx 權重"
    return (
        f"量化面總分 {score.composite:.1f}（{score.grade}），目前最需要關注的是 {weak_dims}。"
        f"ProblemMap 主路由為 {route_summary.get('primary_family_zh', '未解析')}，"
        f"第一優先候選是「{pm_label}」。"
        f"加權後最顯著的 Fx 為 {top_fx}。"
    )


def _aggregate_weighted_dimensions(summaries: List[DiagnosisSummary]) -> List[Dict[str, Any]]:
    dimension_rows: Dict[str, List[Dict[str, Any]]] = {}
    for summary in summaries:
        for row in summary.weighted_dimensions:
            dimension_rows.setdefault(row["dimension"], []).append(row)

    aggregated: List[Dict[str, Any]] = []
    for dimension, rows in dimension_rows.items():
        avg_raw = sum(float(row["raw_score"]) for row in rows) / len(rows)
        avg_attention = sum(float(row["combined_attention"]) for row in rows) / len(rows)
        fx_counter: Dict[str, float] = {}
        for row in rows:
            for contributor in row.get("contributors", []):
                fx_counter[contributor["fx"]] = fx_counter.get(contributor["fx"], 0.0) + float(contributor["weight"])
        top_fx = sorted(fx_counter.items(), key=lambda item: (-item[1], item[0]))[:3]
        aggregated.append(
            {
                "dimension": dimension,
                "dimension_zh": DIMENSION_LABELS_ZH.get(dimension, dimension),
                "raw_score": round(avg_raw, 1),
                "combined_attention": round(avg_attention, 4),
                "combined_attention_pct": f"{avg_attention * 100:.1f}%",
                "fx_weight_ratio_zh": "、".join(
                    f"{fx} {(weight / len(rows)) * 100:.1f}%"
                    for fx, weight in top_fx
                ) or "無顯著 Fx 加權",
                "contributors": [
                    {
                        "fx": fx,
                        "weight": round(weight / len(rows), 4),
                        "weight_pct": f"{(weight / len(rows)) * 100:.1f}%",
                    }
                    for fx, weight in top_fx
                ],
            }
        )
    aggregated.sort(key=lambda item: (-item["combined_attention"], item["dimension"]))
    return aggregated


def _aggregate_batch_pm_candidates(
    summaries: List[DiagnosisSummary],
    aggregated_fx: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    counter = Counter(
        (candidate["number"], candidate["label"], candidate["label_zh"])
        for summary in summaries
        for candidate in summary.pm_candidates
    )
    fx_lookup = {item["fx"]: item for item in aggregated_fx}
    entries: List[Dict[str, Any]] = []
    for index, ((number, label, label_zh), count) in enumerate(counter.most_common(3), start=1):
        guide = PM_FIELD_GUIDE[index - 1]
        linked_families = []
        for family in ATLAS_FAMILIES:
            if int(number) not in family["pm1_support"]:
                continue
            fx_info = fx_lookup.get(family["id"], {})
            linked_families.append(
                {
                    "fx": family["id"],
                    "family_label_zh": _format_family_label_zh(family),
                    "weight": float(fx_info.get("weight", 0.0)),
                    "weight_pct": fx_info.get("weight_pct", "0.0%"),
                }
            )
        linked_families.sort(key=lambda item: (-item["weight"], item["fx"]))
        entries.append(
            {
                "field": guide["field"],
                "field_meaning_zh": f"{guide['meaning_zh']}（批次統計版本）",
                "number": number,
                "label": label,
                "label_zh": label_zh,
                "occurrence_count": count,
                "occurrence_ratio_zh": f"{(count / len(summaries)) * 100:.1f}%",
                "fx_links": linked_families,
                "fx_weight_ratio_zh": "、".join(
                    f"{item['fx']} {item['weight_pct']}" for item in linked_families if item["weight"] > 0
                ) or "目前沒有顯著 Fx 權重",
            }
        )
    return entries


def _aggregate_batch_route_summary(reports: List[Any]) -> Dict[str, Any]:
    primary_counter = Counter(
        getattr(report.problemmap, "atlas", {}).get("primary_family_zh", "未解析")
        for report in reports
        if getattr(report, "problemmap", None) is not None
    )
    secondary_counter = Counter(
        getattr(report.problemmap, "atlas", {}).get("secondary_family_zh", "無")
        for report in reports
        if getattr(report, "problemmap", None) is not None
    )
    return {
        "top_primary_families": [
            {"label_zh": label, "count": count}
            for label, count in primary_counter.most_common(3)
        ],
        "top_secondary_families": [
            {"label_zh": label, "count": count}
            for label, count in secondary_counter.most_common(3)
        ],
    }


def _aggregate_batch_supporting_evidence(summaries: List[DiagnosisSummary]) -> Dict[str, Any]:
    signal_counter = Counter(
        signal["signal_zh"]
        for summary in summaries
        for signal in summary.supporting_evidence.get("failure_signals", [])
    )
    return {
        "failure_signals": [
            {"signal_zh": label, "count": count}
            for label, count in signal_counter.most_common(5)
        ],
    }


def _aggregate_batch_quantitative_summary(
    reports: List[Any],
    summaries: List[DiagnosisSummary],
) -> Dict[str, Any]:
    weak_counter = Counter(
        item["dimension_zh"]
        for summary in summaries
        for item in summary.quantitative_summary.get("weak_dimensions", [])
    )
    composites = [getattr(report.score, "composite", 0.0) for report in reports]
    return {
        "average_composite": round(sum(composites) / len(composites), 1) if composites else 0.0,
        "top_weak_dimensions": [
            {"dimension_zh": label, "count": count}
            for label, count in weak_counter.most_common(5)
        ],
    }


def _build_batch_summary_text_zh(
    route_summary: Dict[str, Any],
    weighted_dimensions: List[Dict[str, Any]],
    fx_weights: List[Dict[str, Any]],
) -> str:
    top_routes = "、".join(
        f"{item['label_zh']} x{item['count']}"
        for item in route_summary.get("top_primary_families", [])[:2]
    ) or "未解析"
    top_dims = "、".join(
        f"{item['dimension_zh']} {item['combined_attention_pct']}"
        for item in weighted_dimensions[:2]
    ) or "無"
    top_fx = "、".join(
        f"{item['fx']} {item['weight_pct']}"
        for item in fx_weights[:2]
        if float(item.get("weight", 0.0)) > 0
    ) or "無顯著 Fx 權重"
    return (
        f"這批 session 最常見的主家族是 {top_routes}。"
        f"加權後最值得優先關注的維度為 {top_dims}，"
        f"主要 Fx 權重集中在 {top_fx}。"
    )


def _format_family_label(family: Dict[str, Any]) -> str:
    return f"{family['id']} {family['name']}"


def _format_family_label_zh(family: Dict[str, Any]) -> str:
    return f"{family['id']} {family['name_zh']}"


def _translate_confidence_zh(value: str) -> str:
    return CONFIDENCE_LABELS_ZH.get(value, value)


def _translate_evidence_sufficiency_zh(value: str) -> str:
    return EVIDENCE_SUFFICIENCY_LABELS_ZH.get(value, value)


def _first_nonempty(values: Any) -> str:
    for value in values:
        if value:
            return str(value)
    return ""


def _clip_text(text: str, limit: int) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 1] + "…"
