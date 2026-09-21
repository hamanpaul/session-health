"""Validate and adapt analysis supplied by the currently running agent.

The contract is intentionally transport-only: callers pass one bounded JSON
object on stdin.  No prompt/result handoff files or executable templates are
accepted here.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, Mapping, Optional, Set

from .agent_analysis import AgentAnalysis
from .semantic_backend import SemanticUsage


MAX_TRIGGER_ANALYSIS_BYTES = 128_000
SECTIONS = ("observations", "hypotheses", "claims", "recommendations")
_ROOT_KEYS = set(SECTIONS) | {"actual_model", "provider", "native_usage"}
_ITEM_KEYS = {"text", "evidence_refs", "counterevidence_refs"}


def collect_evidence_refs(value: Any) -> Set[str]:
    """Collect report-owned reference IDs from a bounded stage-2 context."""

    refs: Set[str] = set()

    def visit(item: Any, key: str = "") -> None:
        if isinstance(item, Mapping):
            for child_key, child in item.items():
                visit(child, str(child_key))
        elif isinstance(item, (list, tuple)):
            if key in {"evidence_refs", "counterevidence_refs", "source_refs"}:
                refs.update(str(child) for child in item if isinstance(child, (str, int)))
            else:
                for child in item:
                    visit(child, key)

    visit(value)
    return refs


def _validate_refs(raw: Any, *, field: str, allowed_refs: Set[str]) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 20:
        raise ValueError(f"{field} must be an array of at most 20 reference IDs")
    refs: list[str] = []
    for value in raw:
        if not isinstance(value, (str, int)):
            raise ValueError(f"{field} contains a non-scalar reference ID")
        ref = str(value)
        if len(ref) > 300:
            raise ValueError(f"{field} contains an overlong reference ID")
        if ref not in allowed_refs:
            raise ValueError(f"unknown evidence reference: {ref}")
        refs.append(ref)
    return refs


def _validate_section(raw: Any, *, section: str, allowed_refs: Set[str]) -> list[Dict[str, Any]]:
    if raw is None:
        return []
    if not isinstance(raw, list) or len(raw) > 100:
        raise ValueError(f"{section} must be an array of at most 100 items")
    result: list[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"{section}[{index}] must be an object")
        unknown = set(item) - _ITEM_KEYS
        if unknown:
            raise ValueError(f"{section}[{index}] has unsupported fields: {sorted(unknown)}")
        text = item.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 12_000:
            raise ValueError(f"{section}[{index}].text must be 1..12000 characters")
        result.append(
            {
                "text": text.strip(),
                "evidence_refs": _validate_refs(
                    item.get("evidence_refs"), field=f"{section}[{index}].evidence_refs", allowed_refs=allowed_refs
                ),
                "counterevidence_refs": _validate_refs(
                    item.get("counterevidence_refs"),
                    field=f"{section}[{index}].counterevidence_refs",
                    allowed_refs=allowed_refs,
                ),
            }
        )
    return result


def _render_text(structured: Mapping[str, Any]) -> str:
    labels = {
        "observations": "Observations",
        "hypotheses": "Hypotheses",
        "claims": "Claims",
        "recommendations": "Recommendations",
    }
    lines: list[str] = []
    for section in SECTIONS:
        items = structured.get(section, [])
        if not items:
            continue
        lines.append(f"### {labels[section]}")
        lines.extend(f"- {item['text']}" for item in items)
    return "\n".join(lines)


def parse_trigger_analysis(
    raw: bytes | str,
    *,
    allowed_refs: Iterable[str] = (),
    requested_model: Optional[str] = None,
) -> AgentAnalysis:
    """Return a report-ready analysis or raise a bounded validation error."""

    encoded = raw if isinstance(raw, bytes) else raw.encode("utf-8")
    if not encoded:
        raise ValueError("trigger-agent analysis stdin is empty")
    if len(encoded) > MAX_TRIGGER_ANALYSIS_BYTES:
        raise ValueError("trigger-agent analysis exceeds 128000 bytes")
    try:
        payload = json.loads(encoded)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("trigger-agent analysis must be one valid JSON object") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("trigger-agent analysis must be one JSON object")
    unknown = set(payload) - _ROOT_KEYS
    if unknown:
        raise ValueError(f"trigger-agent analysis has unsupported fields: {sorted(unknown)}")

    allowed = {str(item) for item in allowed_refs}
    structured = {
        section: _validate_section(payload.get(section, []), section=section, allowed_refs=allowed)
        for section in SECTIONS
    }
    if not any(structured.values()):
        raise ValueError("trigger-agent analysis must contain at least one finding")

    actual_model = payload.get("actual_model")
    if actual_model is not None and (not isinstance(actual_model, str) or len(actual_model) > 200):
        raise ValueError("actual_model must be a short string or null")
    provider = payload.get("provider")
    if provider is not None and (not isinstance(provider, str) or len(provider) > 100):
        raise ValueError("provider must be a short string or null")
    usage = SemanticUsage.from_payload(payload.get("native_usage")).to_dict()
    claims = list(structured["observations"] + structured["hypotheses"] + structured["claims"])
    return AgentAnalysis(
        agent_name="trigger-agent",
        raw_response=_render_text(structured),
        success=True,
        requested_model=requested_model,
        actual_model=actual_model.strip() if isinstance(actual_model, str) and actual_model.strip() else None,
        native_usage=usage,
        structured_output=structured,
        claims=claims,
        recommendations=list(structured["recommendations"]),
        analysis_origin="trigger_agent",
        routing_mode="interactive",
        fallback_policy="disabled",
        diagnostics=[
            {
                "kind": "trigger_agent_analysis_accepted",
                "status": "complete",
                "provider": provider,
            }
        ],
    )
