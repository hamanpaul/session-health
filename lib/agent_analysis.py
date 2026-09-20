"""Bounded second-stage analyzer adapters and model catalog.

The legacy ``call_agent`` entry point remains available, but its production
path now routes concrete executor/provider/route/model/settings cards.  CLI
presence is only read-only discovery evidence; it is never reported as account
availability.  Analyzer prompts are sent through stdin and never placed in
argv, while requested and provider-reported actual identities stay separate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import copy
import html
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .jev_routing import (
    AnalysisRequest,
    RouteDecision,
    RoutingBudget,
    choose_model,
    mark_execution_failure,
    candidate_id as routing_candidate_id,
    catalog_payload,
    routing_vs_baseline,
    run_routing_baseline_pilot,
)
from .postcheck import postcheck_analysis
from .semantic_backend import SemanticUsage
from .scorer import SessionScore
from .parser_base import Session


MAX_ANALYSIS_INPUT_BYTES = 192_000
MAX_ANALYSIS_OUTPUT_BYTES = 128_000
DEFAULT_ANALYSIS_TIMEOUT = 180
CATALOG_FRESHNESS_SECONDS = 24 * 60 * 60


def _checked_at() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _freshness(checked_at: str, *, ttl_seconds: int = CATALOG_FRESHNESS_SECONDS) -> Dict[str, Any]:
    try:
        parsed = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        parsed = datetime.now(timezone.utc)
    return {
        "checked_at": checked_at,
        "expires_at": (parsed + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
        "freshness_seconds": ttl_seconds,
        "stale": False,
    }


def _executable_for(executor: str) -> str:
    return {"codex": "codex", "copilot": "copilot", "agy": "agy"}.get(executor, executor)


def _discovered_availability(executor: str, *, checked_at: Optional[str] = None) -> Dict[str, Any]:
    """Observe executable presence without claiming account/model access."""

    checked = checked_at or _checked_at()
    executable = _executable_for(executor)
    present = bool(shutil.which(executable))
    value = {
        "status": "unknown" if present else "unavailable",
        "provenance": "read_only_discovery",
        "cli_present": present,
        "discovery": "executable_presence_only",
        "executor": executor,
    }
    value.update(_freshness(checked))
    return value


def _operator_availability(
    *,
    status: str = "available",
    checked_at: Optional[str] = None,
    expires_at: Optional[str] = None,
    note: str = "",
) -> Dict[str, Any]:
    if status not in {"available", "unavailable", "unknown"}:
        raise ValueError("operator availability status must be available, unavailable, or unknown")
    checked = checked_at or _checked_at()
    value = {
        "status": status,
        "provenance": "operator",
        "discovery": "operator_entry",
    }
    value.update(_freshness(checked))
    if expires_at:
        value["expires_at"] = expires_at
    if note:
        value["note"] = str(note)[:300]
    return value


@dataclass
class AgentConfig:
    """One concrete analyzer candidate."""

    name: str
    build_cmd: Callable[[str], List[str]]
    timeout: int = DEFAULT_ANALYSIS_TIMEOUT
    executor: str = ""
    provider: str = ""
    route: str = ""
    model_id: str = ""
    inference_settings: Dict[str, Any] = field(default_factory=dict)
    availability: Optional[Dict[str, Any]] = None
    context_window: Optional[int] = 192_000
    max_output_bytes: Optional[int] = MAX_ANALYSIS_OUTPUT_BYTES
    latency_seconds: Optional[float] = None
    cost_per_request: Optional[float] = None
    capabilities: Tuple[str, ...] = ("text", "zh-TW")
    output_formats: Tuple[str, ...] = ("text", "json")
    priority: int = 100
    legacy: bool = False

    def __post_init__(self) -> None:
        if not self.executor:
            self.executor = self.name.split("/", 1)[0]
        if not self.provider:
            self.provider = self.executor
        if not self.route:
            self.route = self.name
        if not self.model_id:
            self.model_id = self.name.split("/", 1)[1] if "/" in self.name else self.name
        self.inference_settings = dict(self.inference_settings or {})
        self.capabilities = tuple(str(item) for item in (self.capabilities or ()))
        self.output_formats = tuple(str(item) for item in (self.output_formats or ()))
        if self.availability is None:
            self.availability = _discovered_availability(self.executor)
        else:
            self.availability = dict(self.availability)
            self.availability.setdefault("status", "unknown")
            self.availability.setdefault("provenance", "operator")
            self.availability.setdefault("checked_at", _checked_at())
            self.availability.setdefault("freshness_seconds", CATALOG_FRESHNESS_SECONDS)
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")

    @property
    def candidate_id(self) -> str:
        settings = json.dumps(self.inference_settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = __import__("hashlib").sha256(settings.encode("utf-8")).hexdigest()[:10]
        return f"{self.executor}/{self.provider}/{self.route}/{self.model_id}/{digest}"

    @property
    def identity_tuple(self) -> Tuple[str, str, str, str, Dict[str, Any]]:
        return (self.executor, self.provider, self.route, self.model_id, dict(self.inference_settings))

    def clone(self) -> "AgentConfig":
        return copy.deepcopy(self)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "candidate_id": self.candidate_id,
            "executor": self.executor,
            "provider": self.provider,
            "route": self.route,
            "model_id": self.model_id,
            "inference_settings": dict(self.inference_settings),
            "availability": dict(self.availability or {}),
            "context_window": self.context_window,
            "max_output_bytes": self.max_output_bytes,
            "latency_seconds": self.latency_seconds,
            "cost_per_request": self.cost_per_request,
            "capabilities": list(self.capabilities),
            "output_formats": list(self.output_formats),
            "priority": self.priority,
            "legacy": self.legacy,
        }


def _build_codex_cmd(_prompt: str) -> List[str]:
    return ["codex", "-c", "model=gpt-5.4", "-c", "model_reasoning_effort=high", "exec", "-"]


def _build_copilot_sonnet_cmd(_prompt: str) -> List[str]:
    return ["copilot", "-s", "--model", "claude-sonnet-4.6", "-p", "-"]


def _build_agy_cmd(_prompt: str) -> List[str]:
    # With text input format, agy reads the bounded prompt from stdin.  Do not
    # append a positional prompt (including ``-``): the installed CLI ignores
    # command-line prompts when ``--input-format text`` is selected.
    # Keep JSON output so native usage remains available when the provider
    # reports it; the parser accepts its ``response`` field below.
    return [
        "agy",
        "--model",
        "gemini-3.8-flash-high",
        "--effort",
        "high",
        "--input-format",
        "text",
        "--output-format",
        "json",
        "--print",
    ]


def _build_gemini_cmd(_prompt: str) -> List[str]:
    """Legacy compatibility adapter; agy never aliases this executor."""

    return ["gemini", "-m", "gemini-3-pro-preview", "-p", "-"]


def _build_copilot_mini_cmd(_prompt: str) -> List[str]:
    return ["copilot", "-s", "--model", "gpt-5-mini", "-p", "-"]


def _catalog_seed() -> List[AgentConfig]:
    return [
        AgentConfig("codex/gpt-5.4", _build_codex_cmd, timeout=180, executor="codex", provider="openai", route="codex.exec", model_id="gpt-5.4", inference_settings={"effort": "high", "stdin": True}, priority=10),
        AgentConfig("copilot/sonnet-4.6", _build_copilot_sonnet_cmd, timeout=150, executor="copilot", provider="github", route="copilot.prompt", model_id="claude-sonnet-4.6", inference_settings={"stdin": True}, priority=20),
        AgentConfig("agy/gemini-3.8-flash-high", _build_agy_cmd, timeout=150, executor="agy", provider="google", route="agy.prompt", model_id="gemini-3.8-flash-high", inference_settings={"effort": "high", "stdin": True}, priority=30),
        AgentConfig("copilot/gpt-5-mini", _build_copilot_mini_cmd, timeout=90, executor="copilot", provider="github", route="copilot.prompt", model_id="gpt-5-mini", inference_settings={"stdin": True}, priority=40),
    ]


AGENT_CHAIN: List[AgentConfig] = _catalog_seed()

# Kept out of the production catalog so the new router cannot silently treat
# the old Gemini CLI as agy.  Callers that explicitly need legacy behavior may
# pass this chain to ``call_agent``.
LEGACY_AGENT_CHAIN: List[AgentConfig] = [
    AgentConfig(
        "gemini/3-pro",
        _build_gemini_cmd,
        timeout=120,
        executor="gemini",
        provider="google",
        route="gemini.prompt",
        model_id="gemini-3-pro-preview",
        inference_settings={"stdin": True, "legacy": True},
        legacy=True,
        priority=90,
    )
]
TEST_AGENT = AgentConfig(
    "copilot/gpt-5-mini (test)",
    _build_copilot_mini_cmd,
    timeout=90,
    executor="copilot",
    provider="github",
    route="copilot.prompt",
    model_id="gpt-5-mini",
    inference_settings={"stdin": True, "test_only": True},
    availability=_operator_availability(status="available", note="explicit test fixture"),
    priority=1,
)


def discover_agent_catalog(
    candidates: Optional[Sequence[AgentConfig]] = None,
    *,
    operator_entries: Optional[Mapping[str, Any] | Sequence[Mapping[str, Any]]] = None,
) -> List[AgentConfig]:
    """Refresh executable presence and apply explicit operator entries."""

    result = [(item.clone()) for item in (candidates or _catalog_seed())]
    for item in result:
        item.availability = _discovered_availability(item.executor)
    if isinstance(operator_entries, Mapping):
        entries = [dict(value, name=key) if isinstance(value, Mapping) else {"name": key, "status": value} for key, value in operator_entries.items()]
    else:
        entries = [entry for entry in (operator_entries or ()) if isinstance(entry, Mapping)]
    for entry in entries:
        wanted = str(entry.get("name", entry.get("candidate_id", entry.get("model_id", ""))))
        for item in result:
            if wanted not in {item.name, item.candidate_id, item.model_id}:
                continue
            item.availability = _operator_availability(
                status=str(entry.get("status", "available")),
                checked_at=str(entry.get("checked_at", "")) or None,
                expires_at=str(entry.get("expires_at", "")) or None,
                note=str(entry.get("note", "")),
            )
            break
    return result


def operator_catalog(entries: Mapping[str, Any] | Sequence[Mapping[str, Any]], *, candidates: Optional[Sequence[AgentConfig]] = None) -> List[AgentConfig]:
    return discover_agent_catalog(candidates, operator_entries=entries)


read_only_discover = discover_agent_catalog


@dataclass
class AgentAnalysis:
    """Result of one bounded analyzer execution."""

    agent_name: str = ""
    raw_response: str = ""
    success: bool = False
    error: str = ""
    requested_model: Optional[str] = None
    actual_model: Optional[str] = None
    requested_settings: Dict[str, Any] = field(default_factory=dict)
    actual_settings: Dict[str, Any] = field(default_factory=dict)
    native_usage: Dict[str, Optional[int]] = field(default_factory=lambda: SemanticUsage().to_dict())
    usage_scope: str = "analyzer_invocation_native"
    structured_output: Dict[str, Any] = field(default_factory=dict)
    claims: List[Dict[str, Any]] = field(default_factory=list)
    recommendations: List[Dict[str, Any]] = field(default_factory=list)
    routing: Optional[RouteDecision] = None
    attempts: List[Dict[str, Any]] = field(default_factory=list)
    postcheck: Any = None
    coverage: Dict[str, Any] = field(default_factory=dict)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_name": self.agent_name,
            "success": self.success,
            "raw_response": self.raw_response,
            "error": self.error,
            "requested_model": self.requested_model,
            "actual_model": self.actual_model,
            "requested_settings": dict(self.requested_settings),
            "actual_settings": dict(self.actual_settings),
            "native_usage": dict(self.native_usage),
            "usage_scope": self.usage_scope,
            "structured_output": dict(self.structured_output),
            "claims": list(self.claims),
            "recommendations": list(self.recommendations),
            "routing": self.routing.to_dict() if self.routing is not None else None,
            "attempts": list(self.attempts),
            "postcheck": self.postcheck.to_dict() if hasattr(self.postcheck, "to_dict") else self.postcheck,
            "coverage": dict(self.coverage),
            "diagnostics": list(self.diagnostics),
        }


def _redact_error(value: Any) -> str:
    text = str(value)
    for marker in ("TYPESAFE_API_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "Authorization"):
        text = text.replace(marker, "[redacted]")
    return text[:300]


_SECRET_VALUE = re.compile(r"(?i)(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret|authorization)\s*[:=]\s*([^\s,;]+)")
_ABSOLUTE_PATH = re.compile(r"(?:^|[\s(])(?:/home/[^\s)]+|/Users/[^\s)]+|[A-Za-z]:[\\/][^\s)]+)")


def _redact_text(value: str) -> str:
    value = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return _ABSOLUTE_PATH.sub(lambda match: match.group(0)[:1] + "[path-redacted]", value)


def _redact_value(value: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        for key, item in list(value.items())[:200]:
            key_text = str(key)
            if any(marker in key_text.lower() for marker in ("key", "token", "password", "secret", "authorization", "cookie")):
                output[key_text] = "[redacted]"
            else:
                output[key_text] = _redact_value(item, depth + 1)
        return output
    if isinstance(value, list):
        return [_redact_value(item, depth + 1) for item in value[:200]]
    if isinstance(value, str):
        return _redact_text(value[:12_000])
    return value


def _parse_structured_output(output: str) -> Tuple[str, Dict[str, Any], Optional[str], Dict[str, Any], Dict[str, Optional[int]]]:
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return output, {}, None, {}, SemanticUsage().to_dict()
    if not isinstance(payload, Mapping):
        return output, {}, None, {}, SemanticUsage().to_dict()
    structured = _redact_value(dict(payload))
    text = payload.get(
        "text",
        payload.get("response", payload.get("analysis", payload.get("output", ""))),
    )
    if not isinstance(text, str):
        text = output
    text = _redact_text(text)
    actual_model = payload.get("actual_model", payload.get("model", payload.get("model_id")))
    actual_model = actual_model.strip() if isinstance(actual_model, str) and actual_model.strip() else None
    actual_settings = payload.get("actual_settings", payload.get("settings", {}))
    actual_settings = _redact_value(dict(actual_settings)) if isinstance(actual_settings, Mapping) else {}
    usage = SemanticUsage.from_payload(payload.get("usage", payload.get("native_usage"))).to_dict()
    return text, structured, actual_model, actual_settings, usage


def _structured_items(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    result: List[Dict[str, Any]] = []
    for item in value[:100]:
        if isinstance(item, Mapping):
            raw = dict(item)
            text = raw.get("text", raw.get("claim", raw.get("recommendation", "")))
        elif isinstance(item, str):
            raw, text = {}, item
        else:
            continue
        if isinstance(text, str) and text.strip():
            raw["text"] = text.strip()[:12_000]
            result.append(raw)
    return result


def _execute_candidate(prompt: str, candidate: AgentConfig, request: AnalysisRequest) -> AgentAnalysis:
    if len(prompt.encode("utf-8")) > request.budget.max_context_bytes:
        return AgentAnalysis(agent_name=candidate.name, success=False, error="analysis input exceeds routing context budget", requested_model=candidate.model_id, requested_settings=dict(candidate.inference_settings), diagnostics=[{"kind": "input_budget_exceeded", "status": "failed"}])
    try:
        argv = candidate.build_cmd("")
        if not isinstance(argv, list) or not argv or any(not isinstance(arg, str) for arg in argv):
            raise ValueError("candidate command adapter must return a string argv list")
        result = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=min(candidate.timeout, int(request.budget.max_latency_seconds)),
            env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"},
            check=False,
        )
    except subprocess.TimeoutExpired:
        return AgentAnalysis(agent_name=candidate.name, success=False, error="analyzer timed out; execution outcome is unknown", requested_model=candidate.model_id, requested_settings=dict(candidate.inference_settings), diagnostics=[{"kind": "timeout", "status": "unknown"}])
    except FileNotFoundError:
        return AgentAnalysis(agent_name=candidate.name, success=False, error="analyzer executable is unavailable", requested_model=candidate.model_id, requested_settings=dict(candidate.inference_settings), diagnostics=[{"kind": "executable_unavailable", "status": "failed"}])
    except Exception as exc:
        return AgentAnalysis(agent_name=candidate.name, success=False, error=_redact_error(f"{type(exc).__name__}: {exc}"), requested_model=candidate.model_id, requested_settings=dict(candidate.inference_settings), diagnostics=[{"kind": "adapter_exception", "status": "failed"}])

    raw_output = result.stdout or ""
    if isinstance(raw_output, bytes):
        raw_output = raw_output.decode("utf-8", errors="replace")
    output = str(raw_output).strip()
    output_size = len(output.encode("utf-8"))
    base = {"agent_name": candidate.name, "requested_model": candidate.model_id, "requested_settings": dict(candidate.inference_settings)}
    if output_size > request.budget.max_output_bytes:
        clipped = output.encode("utf-8")[: request.budget.max_output_bytes].decode("utf-8", errors="replace")
        return AgentAnalysis(**base, raw_response=clipped, success=False, error="analyzer output exceeds routing output budget", diagnostics=[{"kind": "output_budget_exceeded", "status": "failed", "bytes": output_size}])
    if result.returncode != 0:
        return AgentAnalysis(**base, raw_response=output, success=False, error=f"analyzer exited with code {result.returncode}", diagnostics=[{"kind": "execution_failed", "status": "failed", "exit_code": result.returncode}])
    if not output:
        return AgentAnalysis(**base, success=False, error="analyzer returned empty output", diagnostics=[{"kind": "empty_output", "status": "failed"}])
    text, structured, actual_model, actual_settings, usage = _parse_structured_output(output)
    return AgentAnalysis(**base, raw_response=text, success=True, actual_model=actual_model, actual_settings=actual_settings, native_usage=usage, structured_output=structured, claims=_structured_items(structured.get("claims")), recommendations=_structured_items(structured.get("recommendations")), diagnostics=[{"kind": "execution_complete", "status": "complete"}])


def call_agent(
    prompt: str,
    agent_chain: Optional[List[AgentConfig]] = None,
    test_mode: bool = False,
    *,
    routing_backend: Any = None,
    routing_budget: Any = None,
    backend: Any = None,
    model_override: str = "",
    request: Optional[AnalysisRequest] = None,
    use_jev: Optional[bool] = None,
    max_retries: int = 1,
    max_output_bytes: int = MAX_ANALYSIS_OUTPUT_BYTES,
) -> AgentAnalysis:
    """Route and execute one analyzer with bounded failed-execution retry."""

    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    source_chain = [TEST_AGENT] if test_mode else (agent_chain if agent_chain is not None else AGENT_CHAIN)
    candidates = [item.clone() for item in source_chain]
    explicit_chain = agent_chain is not None or test_mode
    chosen_backend = routing_backend if routing_backend is not None else backend
    effective_request = request or AnalysisRequest(
        context_bytes=len(prompt.encode("utf-8")),
        output_bytes=max_output_bytes,
        model_override=model_override,
        budget=RoutingBudget(max_context_bytes=MAX_ANALYSIS_INPUT_BYTES, max_output_bytes=max_output_bytes, max_reselections=max_retries),
    )
    if model_override and not effective_request.model_override:
        effective_request = AnalysisRequest(
            purpose=effective_request.purpose,
            context_bytes=effective_request.context_bytes,
            output_bytes=effective_request.output_bytes,
            max_latency_seconds=effective_request.max_latency_seconds,
            max_cost=effective_request.max_cost,
            required_capabilities=effective_request.required_capabilities,
            allowed_executors=effective_request.allowed_executors,
            output_format=effective_request.output_format,
            language=effective_request.language,
            model_override=model_override,
            inference_settings=effective_request.inference_settings,
            budget=effective_request.budget,
        )
    jev_enabled = bool(chosen_backend is not None) if use_jev is None else use_jev
    excluded: List[str] = []
    attempts: List[Dict[str, Any]] = []
    last: Optional[AgentAnalysis] = None
    max_rounds = min(max_retries, effective_request.budget.max_reselections) + 1
    for round_index in range(max_rounds):
        decision = choose_model(candidates, effective_request, backend=chosen_backend, budget=routing_budget, explicit_override=effective_request.model_override, allow_unknown=explicit_chain, use_jev=jev_enabled, exclude=excluded)
        decision.reselection_count = round_index
        if decision.candidate is None:
            return AgentAnalysis(success=False, error="no suitable analyzer model", requested_model=effective_request.model_override or None, routing=decision, attempts=attempts, diagnostics=decision.diagnostics)
        print(f"  🤖 Calling {decision.candidate.name}...", file=sys.stderr, end="", flush=True)
        analysis = _execute_candidate(prompt, decision.candidate, effective_request)
        analysis.routing = decision
        attempts.append({"candidate_id": routing_candidate_id(decision.candidate), "name": decision.candidate.name, "status": "complete" if analysis.success else (analysis.diagnostics[0].get("status") if analysis.diagnostics else "failed"), "error": analysis.error})
        analysis.attempts = list(attempts)
        last = analysis
        if analysis.success:
            print(" ✓", file=sys.stderr)
            return analysis
        print(" ✗", file=sys.stderr)
        mark_execution_failure(decision.candidate, error_kind=(analysis.diagnostics[0].get("kind") if analysis.diagnostics else "execution_failed"), message=analysis.error)
        excluded.append(routing_candidate_id(decision.candidate))
        if effective_request.model_override:
            break
    return last or AgentAnalysis(success=False, error="all bounded analyzer attempts failed", attempts=attempts)


def prepare_analysis_prompt(score: SessionScore, session: Session, diagnosis_summary: Optional[Dict[str, Any]] = None, problemmap: Optional[Dict[str, Any]] = None, evidence_summary: Optional[Dict[str, Any]] = None) -> str:
    axes = score.radar_axes
    weak_dims = [f"{key}={value:.0f}" for key, value in axes.items() if value < 70]
    user_msgs = [turn.user_input[:200] + ("..." if len(turn.user_input) > 200 else "") for turn in session.turns[:5] if turn.user_input][:3]
    tool_counts: Dict[str, int] = {}
    failures = 0
    for turn in session.turns:
        for call in turn.tool_calls:
            tool_counts[call.name] = tool_counts.get(call.name, 0) + 1
            failures += call.success is False or (call.exit_code is not None and call.exit_code != 0)
    route = diagnosis_summary.get("route_summary", {}) if diagnosis_summary else {}
    diagnosis = "" if not diagnosis_summary else f"\n## 加權診斷\n- 摘要: {diagnosis_summary.get('summary_zh', '無')}\n- 主家族: {route.get('primary_family_zh', '未解析')}\n- 優先修復方向: {route.get('first_fix_zh', '無')}\n"
    if not diagnosis and problemmap:
        atlas = problemmap.get("atlas", {})
        diagnosis = f"\n## ProblemMap\n- 主家族: {atlas.get('primary_family_zh', atlas.get('primary_family', '未解析'))}\n"
    evidence = "" if not evidence_summary else f"\n## Evidence 摘要\n- 弱項: {', '.join(evidence_summary.get('weak_dimensions', {}).keys()) or '無'}\n- Failure signals: {', '.join(evidence_summary.get('candidate_failure_signals', [])[:5]) or '無'}\n- Failed tools: {', '.join(evidence_summary.get('failed_tools', [])[:5]) or '無'}\n"
    return f"""你是一個 Agent CLI Session 品質分析師。只根據下列 bounded facts 提供改善建議；請區分 observations、hypotheses、recommendations，不把推測寫成已驗證事實。

## Session
- ID: {score.session_id}
- source/model: {score.source} / {score.model or 'unknown'}
- turns: {score.turn_count}
- legacy score (compatibility only): {score.composite:.1f}/100 ({score.grade})

## Axes
{' / '.join(f'{key}={value:.0f}' for key, value in axes.items())}
- weak dimensions: {', '.join(weak_dims) or '無'}

## User request samples
{chr(10).join(f'{index + 1}. {value}' for index, value in enumerate(user_msgs)) or '無'}

## Tool facts
- calls: {sum(tool_counts.values())}; failures: {failures}; common: {', '.join(f'{key}({value})' for key, value in sorted(tool_counts.items(), key=lambda item: -item[1])[:5]) or '無'}
- compactions: {score.compaction_count}; aborts: {score.abort_count}
{diagnosis}{evidence}

Return concise observations, bounded hypotheses, and one or two actionable recommendations. Use Traditional Chinese."""


def prepare_batch_analysis_prompt(aggregate: Dict[str, Any], session_summaries: List[Dict[str, Any]], diagnosis_summary: Optional[Dict[str, Any]] = None, *, max_sessions: Optional[int] = None) -> str:
    selected = session_summaries if max_sessions is None else session_summaries[:max_sessions]
    lines = ["- {session_id}: score={score} grade={grade} family={primary} weak={weak} route={route}".format(session_id=item.get("session_id", "unknown"), score=item.get("score", "?"), grade=item.get("grade", "?"), primary=item.get("primary_family", "未解析"), weak=", ".join(item.get("weak_dimensions", [])) or "無", route=item.get("route", "無")) for item in selected]
    return f"""你是一個 Agent CLI Session 品質分析師。請分析全部列出的 session 摘要，不把量化分數或 Jev 判讀當成 correctness proof。

## Batch
- selected sessions: {len(selected)}
- source sessions: {len(session_summaries)}
- omitted by prompt budget: {max(0, len(session_summaries) - len(selected))}
- aggregate: {json.dumps(aggregate, ensure_ascii=False, sort_keys=True)}

## Sessions
{chr(10).join(lines) or '無'}

Return concise observations, recurring bounded hypotheses, and one or two engineering recommendations in Traditional Chinese."""


def render_agent_html_section(analysis: AgentAnalysis) -> str:
    if not analysis.success:
        return ""
    metadata = {"requested_model": analysis.requested_model, "actual_model": analysis.actual_model, "requested_settings": analysis.requested_settings, "actual_settings": analysis.actual_settings, "native_usage": analysis.native_usage, "usage_scope": analysis.usage_scope, "coverage": analysis.coverage, "routing": analysis.routing.to_dict() if analysis.routing else None, "postcheck": analysis.postcheck.to_dict() if hasattr(analysis.postcheck, "to_dict") else analysis.postcheck}
    return f"""<div class="agent-analysis"><h2>🤖 AI 分析報告</h2><div class="agent-meta">分析引擎: <strong>{html.escape(analysis.agent_name)}</strong></div><div class="agent-content">{_markdown_to_html(analysis.raw_response)}</div><details><summary>Routing / identity / usage / post-check</summary><pre>{html.escape(json.dumps(metadata, ensure_ascii=False, indent=2))}</pre></details></div>"""


def render_agent_terminal(analysis: AgentAnalysis) -> str:
    if not analysis.success:
        return ""
    lines = ["", "╔════════════════════════════════════════════════════════╗", f"║  🤖 AI Analysis (via {analysis.agent_name})", f"║  requested={analysis.requested_model or 'unknown'} actual={analysis.actual_model or 'unknown'}", f"║  usage(total)={analysis.native_usage.get('total_tokens') if analysis.native_usage else None}", "╠════════════════════════════════════════════════════════╣"]
    if analysis.routing:
        lines.append(f"║  route={analysis.routing.routing_source} status={analysis.routing.status}")
    for line in analysis.raw_response.split("\n")[:80]:
        if line.strip():
            lines.append(f"║  {line[:52]}{'…' if len(line) > 52 else ''}")
    if analysis.postcheck is not None:
        lines.append(f"║  postcheck={getattr(analysis.postcheck, 'status', 'unknown')} repairs={getattr(analysis.postcheck, 'repair_count', 0)}")
    lines.append("╚════════════════════════════════════════════════════════╝")
    return "\n".join(lines)


def _markdown_to_html(md: str) -> str:
    import re
    output: List[str] = []
    in_list = False
    list_tag = "ul"
    for line in md.split("\n"):
        stripped = line.strip()
        if not stripped:
            if in_list:
                output.append(f"</{list_tag}>")
                in_list = False
            output.append("<br>")
        elif stripped.startswith("### "):
            output.append(f"<h4>{html.escape(stripped[4:])}</h4>")
        elif stripped.startswith("## "):
            output.append(f"<h3>{html.escape(stripped[3:])}</h3>")
        elif stripped.startswith("# "):
            output.append(f"<h3>{html.escape(stripped[2:])}</h3>")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            if not in_list:
                list_tag = "ul"
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{html.escape(stripped[2:])}</li>")
        elif re.match(r"^\d+\.\s", stripped):
            if not in_list:
                list_tag = "ol"
                output.append("<ol>")
                in_list = True
            output.append(f"<li>{html.escape(re.sub(r'^\d+\.\s', '', stripped))}</li>")
        else:
            if in_list:
                output.append(f"</{list_tag}>")
                in_list = False
            output.append(f"<p>{html.escape(stripped)}</p>")
    if in_list:
        output.append(f"</{list_tag}>")
    return "\n".join(output)


# Re-export routing names from the historical module entry point.
ModelCandidate = AgentConfig
RoutingResult = RouteDecision
route_analysis = choose_model
build_agent_catalog = discover_agent_catalog
catalog_json = catalog_payload
