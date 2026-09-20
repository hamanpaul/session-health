"""Bounded model-candidate routing for second-stage session analysis.

The routing layer deliberately knows nothing about credentials.  Discovery can
observe a local executable, but that observation is recorded as ``unknown``
account availability.  Only an operator entry (or an explicit model override)
may make an unknown candidate runnable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


ROUTING_VERSION = "routing-v1"
ROUTING_POLICY_VERSION = "deterministic-priority-v1"
ROUTING_SPECIAL_VALUES = (
    "no_suitable_model",
    "insufficient_model_evidence",
    "mixed",
    "none",
    "insufficient",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    converted = float(value)
    return converted if math.isfinite(converted) else None


@dataclass(frozen=True)
class RoutingBudget:
    """Hard limits applied before a candidate can be selected."""

    max_context_bytes: int = 192_000
    max_output_bytes: int = 128_000
    max_latency_seconds: float = 180.0
    max_cost: Optional[float] = None
    max_reselections: int = 1

    def __post_init__(self) -> None:
        for name in ("max_context_bytes", "max_output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if isinstance(self.max_reselections, bool) or not isinstance(self.max_reselections, int) or self.max_reselections < 0:
            raise ValueError("max_reselections must be a non-negative integer")
        if not isinstance(self.max_latency_seconds, (int, float)) or isinstance(self.max_latency_seconds, bool):
            raise ValueError("max_latency_seconds must be a positive number")
        if not math.isfinite(float(self.max_latency_seconds)) or self.max_latency_seconds <= 0:
            raise ValueError("max_latency_seconds must be a positive number")
        if self.max_cost is not None:
            if not isinstance(self.max_cost, (int, float)) or isinstance(self.max_cost, bool):
                raise ValueError("max_cost must be a finite non-negative number")
            if not math.isfinite(float(self.max_cost)) or self.max_cost < 0:
                raise ValueError("max_cost must be a finite non-negative number")


@dataclass(frozen=True)
class AnalysisRequest:
    """The bounded requirements used to route one analyzer invocation."""

    purpose: str = "session-health-analysis"
    context_bytes: int = 0
    output_bytes: int = 16_384
    max_latency_seconds: Optional[float] = None
    max_cost: Optional[float] = None
    required_capabilities: Tuple[str, ...] = ()
    allowed_executors: Tuple[str, ...] = ()
    output_format: str = "text"
    language: str = "zh-TW"
    model_override: str = ""
    inference_settings: Mapping[str, Any] = field(default_factory=dict)
    budget: RoutingBudget = field(default_factory=RoutingBudget)

    def __post_init__(self) -> None:
        for name in ("context_bytes", "output_bytes"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("max_latency_seconds", "max_cost"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)) or value < 0:
                    raise ValueError(f"{name} must be a finite non-negative number")
        if not isinstance(self.inference_settings, Mapping):
            raise ValueError("inference_settings must be a mapping")

    @property
    def request_id(self) -> str:
        return _hash(self.to_dict())[:24]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "purpose": self.purpose,
            "context_bytes": self.context_bytes,
            "output_bytes": self.output_bytes,
            "max_latency_seconds": self.max_latency_seconds,
            "max_cost": self.max_cost,
            "required_capabilities": list(self.required_capabilities),
            "allowed_executors": list(self.allowed_executors),
            "output_format": self.output_format,
            "language": self.language,
            "model_override": self.model_override or None,
            "inference_settings": dict(self.inference_settings),
            "budget": {
                "max_context_bytes": self.budget.max_context_bytes,
                "max_output_bytes": self.budget.max_output_bytes,
                "max_latency_seconds": self.budget.max_latency_seconds,
                "max_cost": self.budget.max_cost,
                "max_reselections": self.budget.max_reselections,
            },
        }


@dataclass(frozen=True)
class Eligibility:
    candidate_id: str
    eligible: bool
    reasons: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"candidate_id": self.candidate_id, "eligible": self.eligible, "reasons": list(self.reasons)}


@dataclass
class RouteDecision:
    """A report-safe routing result, including why candidates were rejected."""

    status: str = "no_suitable_model"
    routing_source: str = "none"
    candidate: Any = None
    candidate_id: Optional[str] = None
    eligible_candidate_ids: List[str] = field(default_factory=list)
    eligibility: List[Eligibility] = field(default_factory=list)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    requested_model: Optional[str] = None
    choice_question_id: Optional[str] = None
    choice_value: Optional[str] = None
    jev_status: str = "not_requested"
    jev_usage: Dict[str, Any] = field(default_factory=dict)
    policy_version: str = ROUTING_POLICY_VERSION
    request: Dict[str, Any] = field(default_factory=dict)
    reselection_count: int = 0

    @property
    def selected(self) -> Any:
        return self.candidate

    def to_dict(self) -> Dict[str, Any]:
        candidate_payload = None
        if self.candidate is not None:
            to_dict = getattr(self.candidate, "to_dict", None)
            if callable(to_dict):
                candidate_payload = to_dict()
            else:
                candidate_payload = {"name": str(getattr(self.candidate, "name", self.candidate))}
        return {
            "version": ROUTING_VERSION,
            "status": self.status,
            "routing_source": self.routing_source,
            "candidate": candidate_payload,
            "candidate_id": self.candidate_id,
            "eligible_candidate_ids": list(self.eligible_candidate_ids),
            "eligibility": [item.to_dict() for item in self.eligibility],
            "diagnostics": list(self.diagnostics),
            "requested_model": self.requested_model,
            "choice_question_id": self.choice_question_id,
            "choice_value": self.choice_value,
            "jev_status": self.jev_status,
            "jev_usage": dict(self.jev_usage),
            "policy_version": self.policy_version,
            "request": dict(self.request),
            "reselection_count": self.reselection_count,
        }


@dataclass
class RoutingPilotResult:
    """Selection-agreement pilot; it is not a model-quality gold label."""

    version: str = ROUTING_VERSION
    status: str = "not_applicable"
    cases: List[Dict[str, Any]] = field(default_factory=list)
    routed_selected_count: int = 0
    baseline_selected_count: int = 0
    agreement_count: int = 0
    denominator: int = 0
    agreement: Optional[float] = None
    label_status: str = "synthetic_expectations_only"
    quality_authority: Optional[str] = None
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "cases": list(self.cases),
            "routed_selected_count": self.routed_selected_count,
            "baseline_selected_count": self.baseline_selected_count,
            "agreement_count": self.agreement_count,
            "denominator": self.denominator,
            "agreement": self.agreement,
            "label_status": self.label_status,
            "quality_authority": self.quality_authority,
            "diagnostics": list(self.diagnostics),
        }


def candidate_id(candidate: Any) -> str:
    """Return the stable concrete-candidate identity used in Choice."""

    value = getattr(candidate, "candidate_id", None)
    if isinstance(value, str) and value:
        return value
    identity = getattr(candidate, "identity", None)
    if callable(identity):
        result = identity()
        if isinstance(result, str) and result:
            return result
    name = getattr(candidate, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(candidate)


def _candidate_value(candidate: Any, name: str, default: Any = None) -> Any:
    return getattr(candidate, name, default)


def _availability(candidate: Any) -> Dict[str, Any]:
    value = _candidate_value(candidate, "availability", {})
    return dict(value) if isinstance(value, Mapping) else {}


def _is_stale(availability: Mapping[str, Any]) -> bool:
    if availability.get("stale") is True:
        return True
    freshness = availability.get("freshness")
    if isinstance(freshness, Mapping) and freshness.get("stale") is True:
        return True
    expires = (
        availability.get("expires_at")
        or availability.get("fresh_until")
        or (freshness.get("expires_at") if isinstance(freshness, Mapping) else None)
    )
    if not isinstance(expires, str) or not expires:
        checked = availability.get("checked_at")
        ttl = availability.get("freshness_seconds")
        if isinstance(checked, str) and isinstance(ttl, (int, float)) and not isinstance(ttl, bool):
            try:
                parsed_checked = datetime.fromisoformat(checked.replace("Z", "+00:00"))
                return parsed_checked.timestamp() + float(ttl) <= datetime.now(timezone.utc).timestamp()
            except (TypeError, ValueError, OverflowError):
                return False
        return False
    try:
        parsed = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        return parsed <= datetime.now(timezone.utc)
    except (TypeError, ValueError):
        return False


def evaluate_candidate(
    candidate: Any,
    request: AnalysisRequest,
    *,
    allow_unknown: bool = False,
) -> Eligibility:
    """Apply only hard constraints; no quality score is inferred here."""

    cid = candidate_id(candidate)
    reasons: List[str] = []
    availability = _availability(candidate)
    status = str(availability.get("status", "unknown"))
    if _is_stale(availability):
        reasons.append("availability_stale")
    elif status == "unavailable":
        reasons.append("unavailable")
    elif status == "unknown" and not allow_unknown:
        reasons.append("availability_unknown")
    elif status not in {"available", "unknown"}:
        reasons.append("invalid_availability_status")

    executors = tuple(str(item) for item in (_candidate_value(candidate, "capabilities", ()) or ()))
    if request.allowed_executors and str(_candidate_value(candidate, "executor", "")) not in request.allowed_executors:
        reasons.append("executor_not_allowed")
    missing = [item for item in request.required_capabilities if item not in executors]
    if missing:
        reasons.append("missing_capability:" + ",".join(missing))

    context_window = _candidate_value(candidate, "context_window", None)
    if context_window is None:
        context_window = _candidate_value(candidate, "max_context_bytes", None)
    if request.context_bytes > 0 and context_window is None:
        reasons.append("context_limit_unknown")
    elif context_window is not None and request.context_bytes > int(context_window):
        reasons.append("context_limit_exceeded")

    max_output = _candidate_value(candidate, "max_output_bytes", None)
    if max_output is None:
        max_output = _candidate_value(candidate, "output_limit_bytes", None)
    if request.output_bytes > 0 and max_output is None:
        reasons.append("output_limit_unknown")
    elif max_output is not None and request.output_bytes > int(max_output):
        reasons.append("output_limit_exceeded")

    latency = _finite(_candidate_value(candidate, "latency_seconds", None))
    latency_limit = request.max_latency_seconds if request.max_latency_seconds is not None else request.budget.max_latency_seconds
    timeout = _finite(_candidate_value(candidate, "timeout", None))
    if timeout is not None and timeout > latency_limit:
        reasons.append("latency_budget_exceeded")
    if latency is not None and latency > latency_limit:
        reasons.append("latency_budget_exceeded")

    cost = _finite(_candidate_value(candidate, "cost_per_request", None))
    cost_limit = request.max_cost if request.max_cost is not None else request.budget.max_cost
    if cost_limit is not None:
        if cost is None:
            reasons.append("cost_unknown")
        elif cost > cost_limit:
            reasons.append("cost_budget_exceeded")

    supported_formats = _candidate_value(candidate, "output_formats", ()) or ()
    if supported_formats and request.output_format not in supported_formats:
        reasons.append("output_format_unsupported")

    settings = _candidate_value(candidate, "inference_settings", {})
    if isinstance(settings, Mapping):
        for key, expected in request.inference_settings.items():
            if key in settings and settings[key] != expected:
                reasons.append(f"setting_mismatch:{key}")

    return Eligibility(candidate_id=cid, eligible=not reasons, reasons=tuple(reasons))


def eligible_candidates(
    candidates: Sequence[Any],
    request: AnalysisRequest,
    *,
    allow_unknown: bool = False,
    exclude: Iterable[str] = (),
) -> Tuple[List[Any], List[Eligibility]]:
    """Return eligible candidates and a complete rejection ledger."""

    excluded = set(str(item) for item in exclude)
    accepted: List[Any] = []
    ledger: List[Eligibility] = []
    for item in candidates:
        result = evaluate_candidate(item, request, allow_unknown=allow_unknown)
        if result.candidate_id in excluded:
            result = Eligibility(result.candidate_id, False, result.reasons + ("excluded_after_failure",))
        ledger.append(result)
        if result.eligible:
            accepted.append(item)
    accepted.sort(key=lambda item: (int(_candidate_value(item, "priority", 100)), candidate_id(item)))
    return accepted, ledger


def filter_eligible_candidates(*args: Any, **kwargs: Any) -> List[Any]:
    """Compatibility helper returning only the accepted candidate list."""

    return eligible_candidates(*args, **kwargs)[0]


def _override_match(candidates: Sequence[Any], override: str) -> Optional[Any]:
    wanted = str(override).strip()
    if not wanted:
        return None
    for item in candidates:
        values = {
            candidate_id(item),
            str(_candidate_value(item, "name", "")),
            str(_candidate_value(item, "model_id", "")),
            str(_candidate_value(item, "route", "")),
        }
        if wanted in values:
            return item
    return None


def _choice_question(request: AnalysisRequest, candidates: Sequence[Any]) -> Any:
    from .semantic_backend import SemanticQuestion

    options = tuple(dict.fromkeys(
        [candidate_id(item) for item in candidates]
        + ["no_suitable_model", "insufficient_model_evidence"]
    ))
    return SemanticQuestion(
        question_id=f"route:{request.request_id}",
        axis_id="ROUTE",
        prompt=(
            "Choose exactly one concrete analyzer candidate for this bounded request. "
            "Use only candidates whose hard constraints are satisfied; return no_suitable_model "
            "when none is suitable. Candidate cards contain executor, route, model and settings."
        ),
        answer_type="choice",
        case_id="routing",
        state_path="state.data.routing",
        choices=options,
        group_version=ROUTING_VERSION,
        metadata={"request_id": request.request_id, "policy_version": ROUTING_POLICY_VERSION},
    )


def _candidate_card(candidate: Any) -> Dict[str, Any]:
    to_dict = getattr(candidate, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
        if isinstance(value, Mapping):
            return dict(value)
    return {
        "candidate_id": candidate_id(candidate),
        "executor": _candidate_value(candidate, "executor", ""),
        "provider": _candidate_value(candidate, "provider", ""),
        "route": _candidate_value(candidate, "route", ""),
        "model_id": _candidate_value(candidate, "model_id", ""),
        "inference_settings": dict(_candidate_value(candidate, "inference_settings", {}) or {}),
        "availability": _availability(candidate),
    }


def choose_model(
    candidates: Sequence[Any] | AnalysisRequest,
    request: Optional[AnalysisRequest] | Sequence[Any] = None,
    *,
    backend: Any = None,
    explicit_override: str = "",
    budget: Any = None,
    allow_unknown: bool = False,
    use_jev: bool = True,
    exclude: Iterable[str] = (),
) -> RouteDecision:
    """Select one concrete candidate using Jev Choice or deterministic policy.

    The two positional forms ``choose_model(candidates, request)`` and
    ``choose_model(request, candidates)`` are accepted for small integrations.
    """

    if isinstance(candidates, AnalysisRequest):
        actual_request = candidates
        actual_candidates = request if isinstance(request, Sequence) and not isinstance(request, (str, bytes)) else []
    else:
        actual_candidates = candidates
        actual_request = request if isinstance(request, AnalysisRequest) else AnalysisRequest()
    candidate_list = list(actual_candidates or [])
    override = str(explicit_override or actual_request.model_override or "").strip()
    decision = RouteDecision(
        requested_model=override or None,
        request=actual_request.to_dict(),
    )

    if override:
        selected = _override_match(candidate_list, override)
        if selected is None:
            decision.routing_source = "override"
            decision.diagnostics.append({"kind": "override_not_found", "status": "failed", "model": override})
            return decision
        eligibility = evaluate_candidate(selected, actual_request, allow_unknown=True)
        decision.eligibility = [eligibility]
        decision.eligible_candidate_ids = [eligibility.candidate_id] if eligibility.eligible else []
        if not eligibility.eligible:
            decision.routing_source = "override"
            decision.candidate = selected
            decision.candidate_id = candidate_id(selected)
            decision.diagnostics.append({"kind": "override_ineligible", "status": "failed", "reasons": list(eligibility.reasons)})
            return decision
        decision.status = "selected"
        decision.routing_source = "override"
        decision.candidate = selected
        decision.candidate_id = candidate_id(selected)
        if _availability(selected).get("status") != "available":
            decision.diagnostics.append(
                {
                    "kind": "override_availability_unconfirmed",
                    "status": "unknown",
                    "availability": _availability(selected),
                }
            )
        return decision

    accepted, ledger = eligible_candidates(candidate_list, actual_request, allow_unknown=allow_unknown, exclude=exclude)
    decision.eligibility = ledger
    decision.eligible_candidate_ids = [candidate_id(item) for item in accepted]
    if not accepted:
        decision.diagnostics.append({"kind": "no_eligible_candidate", "status": "insufficient"})
        return decision

    if not use_jev or backend is None:
        selected = accepted[0]
        decision.status = "selected"
        decision.routing_source = "deterministic_fallback"
        decision.candidate = selected
        decision.candidate_id = candidate_id(selected)
        decision.jev_status = "not_requested"
        return decision

    question = _choice_question(actual_request, accepted)
    decision.choice_question_id = question.question_id
    try:
        from .semantic_backend import SemanticBudget, SemanticState

        selected_budget = budget or SemanticBudget(max_requests=1, max_attempts=1, max_questions=1, max_cases=1, max_retries=0)
        state = SemanticState(
            state_id=f"routing-{actual_request.request_id}",
            data={
                "routing": {
                    "request": actual_request.to_dict(),
                    "candidates": [_candidate_card(item) for item in accepted],
                }
            },
            case_ids=("routing",),
        )
        response = backend.evaluate(state, [question], budget=selected_budget)
        decision.jev_status = str(getattr(response, "status", "unknown"))
        usage = getattr(response, "usage", None)
        if usage is not None and hasattr(usage, "to_dict"):
            decision.jev_usage = usage.to_dict()
        answer = getattr(response, "answers", {}).get(question.question_id)
        value = getattr(answer, "value", None)
        decision.choice_value = value if isinstance(value, str) else None
        selected = next(
            (
                item
                for item in accepted
                if value
                in {
                    candidate_id(item),
                    str(_candidate_value(item, "name", "")),
                    str(_candidate_value(item, "model_id", "")),
                }
            ),
            None,
        )
        if selected is not None:
            decision.status = "selected"
            decision.routing_source = "jev"
            decision.candidate = selected
            decision.candidate_id = candidate_id(selected)
            return decision
        if value in ROUTING_SPECIAL_VALUES:
            decision.routing_source = "jev"
            decision.diagnostics.append({"kind": "jev_abstention", "status": "insufficient", "value": value})
            if value in {"no_suitable_model", "insufficient_model_evidence", "none", "insufficient"}:
                decision.status = "no_suitable_model"
                return decision
            # ``mixed`` is an explicit ambiguity and is not allowed to select
            # an arbitrary model under the Jev source.
            decision.status = "no_suitable_model"
            return decision
        elif value is not None:
            decision.routing_source = "jev"
            decision.status = "no_suitable_model"
            decision.diagnostics.append({"kind": "jev_illegal_choice", "status": "failed", "value": value})
            return decision
        else:
            decision.diagnostics.append({"kind": "jev_no_answer", "status": "unknown"})
    except Exception as exc:
        decision.jev_status = "failed"
        decision.diagnostics.append({"kind": "jev_routing_exception", "status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]})

    # A Jev outage or abstention must not make routing nondeterministic.  The
    # fallback is explicit in the report and is still limited to hard-eligible
    # candidates.
    selected = accepted[0]
    decision.status = "selected"
    decision.routing_source = "deterministic_fallback"
    decision.candidate = selected
    decision.candidate_id = candidate_id(selected)
    return decision


def route_analysis(*args: Any, **kwargs: Any) -> RouteDecision:
    """Named alias used by callers that prefer a routing verb."""

    return choose_model(*args, **kwargs)


def routing_vs_baseline(
    requests: AnalysisRequest | Sequence[AnalysisRequest],
    candidates: Sequence[Any],
    *,
    backend: Any = None,
    allow_unknown: bool = False,
) -> RoutingPilotResult:
    """Compare routed selection with deterministic priority on synthetic cases."""

    request_list = [requests] if isinstance(requests, AnalysisRequest) else list(requests)
    result = RoutingPilotResult(denominator=len(request_list))
    for index, request in enumerate(request_list, 1):
        routed = choose_model(candidates, request, backend=backend, allow_unknown=allow_unknown, use_jev=backend is not None)
        baseline = choose_model(candidates, request, allow_unknown=allow_unknown, use_jev=False)
        routed_id = candidate_id(routed.candidate) if routed.candidate is not None else None
        baseline_id = candidate_id(baseline.candidate) if baseline.candidate is not None else None
        if routed.candidate is not None:
            result.routed_selected_count += 1
        if baseline.candidate is not None:
            result.baseline_selected_count += 1
        if routed_id is not None and routed_id == baseline_id:
            result.agreement_count += 1
        result.cases.append(
            {
                "case": index,
                "request_id": request.request_id,
                "routed": routed.to_dict(),
                "baseline": baseline.to_dict(),
                "agreement": routed_id is not None and routed_id == baseline_id,
            }
        )
    if not request_list:
        return result
    result.agreement = result.agreement_count / result.denominator
    result.status = "complete" if result.baseline_selected_count == result.denominator else "partial"
    if result.routed_selected_count < result.denominator:
        result.diagnostics.append({"kind": "routed_selection_incomplete", "status": "partial"})
    result.diagnostics.append(
        {
            "kind": "quality_not_established",
            "status": "not_applicable",
            "message": "selection agreement is a synthetic routing pilot, not a quality or correctness label",
        }
    )
    return result


def run_routing_baseline_pilot(*args: Any, **kwargs: Any) -> RoutingPilotResult:
    return routing_vs_baseline(*args, **kwargs)


@dataclass
class ModelCatalog:
    """Small catalog facade for CLI adapters and embedding callers."""

    candidates: List[Any] = field(default_factory=list)
    provenance: str = "read_only_discovery"
    version: str = ROUTING_VERSION

    @classmethod
    def discover(cls, candidates: Optional[Sequence[Any]] = None, **kwargs: Any) -> "ModelCatalog":
        # Import lazily so the routing core stays independent of CLI adapter
        # construction and avoids an agent_analysis import cycle.
        from .agent_analysis import discover_agent_catalog

        return cls(list(discover_agent_catalog(candidates, **kwargs)))

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "provenance": self.provenance, "candidates": catalog_payload(self.candidates)}

    def find(self, identity: str) -> Optional[Any]:
        return _override_match(self.candidates, identity)


class JevRouter:
    """Object-oriented facade over the deterministic routing functions."""

    def __init__(self, catalog: ModelCatalog | Sequence[Any], *, backend: Any = None, allow_unknown: bool = False) -> None:
        self.catalog = catalog if isinstance(catalog, ModelCatalog) else ModelCatalog(list(catalog))
        self.backend = backend
        self.allow_unknown = allow_unknown

    def choose(self, request: AnalysisRequest, *, explicit_override: str = "", exclude: Iterable[str] = ()) -> RouteDecision:
        return choose_model(
            self.catalog.candidates,
            request,
            backend=self.backend,
            explicit_override=explicit_override,
            allow_unknown=self.allow_unknown,
            use_jev=self.backend is not None,
            exclude=exclude,
        )

    route = choose


def mark_execution_failure(candidate: Any, *, error_kind: str, message: str = "") -> None:
    """Update one mutable catalog card after a bounded execution failure."""

    availability = _availability(candidate)
    previous = dict(availability)
    availability.update(
        {
            "status": "unavailable",
            "provenance": "execution",
            "checked_at": _utc_now(),
            "stale": False,
            "last_error_kind": str(error_kind),
            "last_error": str(message)[:300] if message else "",
            "previous": {"status": previous.get("status"), "provenance": previous.get("provenance")},
        }
    )
    try:
        candidate.availability = availability
    except Exception:
        # Frozen/custom candidate objects are still safe to route around; the
        # caller's exclusion set is the authoritative reselection guard.
        return


def catalog_payload(candidates: Sequence[Any]) -> List[Dict[str, Any]]:
    """Return a stable, portable catalog projection for CLI/report output."""

    return [_candidate_card(item) for item in candidates]


# Friendly names used by downstream integrations.
ModelCandidate = Any
RoutingConstraints = RoutingBudget
RoutingResult = RouteDecision
build_model_catalog = ModelCatalog.discover
select_model = choose_model
check_eligibility = evaluate_candidate
