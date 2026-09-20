"""Typed, bounded semantic evaluation backends.

The semantic layer is deliberately independent from the offline metrics.  It
accepts an already bounded state snapshot and typed questions, then returns
typed judgments plus request-level provenance and usage.  A backend instance
keeps an aggregate ledger for its request/attempt/byte caps, while each
response receives an immutable ledger snapshot for only that ``evaluate``
call; the report layer merges those snapshots within one session.  The HTTP
adapter is standard-library only so importing or running the offline CLI never
requires a model SDK or a network connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import email.utils
import hashlib
import json
import math
import os
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SEMANTIC_VERSION = "semantic-v1"
JEV_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
DEFAULT_JEV_MODEL = "jev-latest"
CHOICE_SPECIAL_VALUES = ("none", "insufficient", "mixed")
SUPPORTED_PRIMITIVES = ("choice", "noul", "score")
BUDGET_SCOPE = "backend_instance_aggregate"
SCORE_PRECISION_POLICY = "reported_decimal_half_unit"


@dataclass(frozen=True)
class Choice:
    """Closed-set Choice schema used by a semantic question."""

    options: Tuple[str, ...]
    include_special: bool = True

    def validate(self, value: Any) -> str:
        if not isinstance(value, str):
            raise SemanticValidationError("Choice value must be a string")
        allowed = set(self.options)
        if self.include_special:
            allowed.update(CHOICE_SPECIAL_VALUES)
        if value not in allowed:
            raise SemanticValidationError(f"Choice value outside closed set: {value}")
        return value

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": "Choice",
            "options": list(self.options),
            "special_values": list(CHOICE_SPECIAL_VALUES) if self.include_special else [],
        }


@dataclass(frozen=True)
class Noul:
    """Noul p(yes) schema; it has no additional confidence field."""

    def validate(self, value: Any) -> float:
        result = _finite_number(value, "Noul p(yes)")
        if result < 0.0 or result > 1.0:
            raise SemanticValidationError("Noul p(yes) must be in [0, 1]")
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {"type": "Noul", "value": "p_yes", "range": [0.0, 1.0]}


@dataclass(frozen=True)
class Score:
    """Ordered Score schema with either named levels or a numeric range."""

    levels: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None

    def validate(self, value: Any) -> Any:
        if self.levels:
            if isinstance(value, str) and value in self.levels:
                return value
            if isinstance(value, bool):
                raise SemanticValidationError("Score value cannot be bool")
            numeric = _finite_number(value, "Score value")
            if 0.0 <= numeric <= len(self.levels) - 1:
                return numeric
            raise SemanticValidationError("Score value is outside ordered levels")
        result = _finite_number(value, "Score value")
        if self.minimum is not None and result < self.minimum:
            raise SemanticValidationError("Score value is below minimum")
        if self.maximum is not None and result > self.maximum:
            raise SemanticValidationError("Score value is above maximum")
        return result

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"type": "Score"}
        if self.levels:
            payload["levels"] = list(self.levels)
        if self.minimum is not None:
            payload["minimum"] = self.minimum
        if self.maximum is not None:
            payload["maximum"] = self.maximum
        return payload


class SemanticError(ValueError):
    """Base error for malformed semantic requests or responses."""


class SemanticValidationError(SemanticError):
    """Raised when a typed request or response violates its contract."""


class SemanticBudgetError(SemanticError):
    """Raised when a semantic request cannot fit a declared budget."""


class SemanticResponseError(SemanticError):
    """Raised when a wire response cannot be decoded after the request ran."""

    def __init__(
        self,
        message: str,
        *,
        error_kind: str = "invalid_response",
        status_code: Optional[int] = None,
        response_hash: str = "",
    ) -> None:
        super().__init__(message)
        self.error_kind = error_kind
        self.status_code = status_code
        self.response_hash = response_hash


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive_int(name: str, value: Any) -> int:
    if not _is_int(value) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _finite_number(value: Any, name: str = "number") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SemanticValidationError(f"{name} must be a finite number")
    converted = float(value)
    if not math.isfinite(converted):
        raise SemanticValidationError(f"{name} must be finite")
    return converted


def canonical_json(value: Any) -> bytes:
    """Encode a request deterministically for byte limits and provenance."""

    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SemanticValidationError(f"value is not portable JSON: {exc}") from exc


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class SemanticBudget:
    """All limits used by one semantic run.

    Byte limits are intentionally conservative UTF-8 request limits.  They are
    not token counts and are reported as bytes in provenance.  A caller can
    lower them for fixtures or raise them explicitly after checking its
    provider contract.
    """

    max_requests: int = 8
    max_attempts: int = 12
    max_questions: int = 64
    max_cases: int = 32
    max_stages: int = 2
    max_state_bytes: int = 192_000
    max_question_bytes: int = 32_000
    max_request_bytes: int = 256_000
    max_total_bytes: int = 1_000_000
    timeout_seconds: float = 30.0
    max_retries: int = 2
    retry_after_cap_seconds: float = 10.0
    max_response_bytes: int = 512_000

    def __post_init__(self) -> None:
        for name in (
            "max_requests",
            "max_attempts",
            "max_questions",
            "max_cases",
            "max_stages",
            "max_state_bytes",
            "max_question_bytes",
            "max_request_bytes",
            "max_total_bytes",
            "max_response_bytes",
        ):
            _positive_int(name, getattr(self, name))
        if not isinstance(self.max_retries, int) or isinstance(self.max_retries, bool) or self.max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer")
        for name in ("timeout_seconds", "retry_after_cap_seconds"):
            value = _finite_number(getattr(self, name), name)
            if value <= 0:
                raise ValueError(f"{name} must be positive")
        if self.max_attempts < self.max_requests:
            raise ValueError("max_attempts must be at least max_requests")


@dataclass(frozen=True)
class SemanticCapabilities:
    """Capabilities advertised by a semantic backend.

    Each primitive is explicitly marked native, emulated, or unsupported;
    generic model confidence is never silently treated as a native Noul.
    """

    backend: str = "generic"
    version: str = SEMANTIC_VERSION
    primitives: Mapping[str, str] = field(
        default_factory=lambda: {name: "native" for name in SUPPORTED_PRIMITIVES}
    )
    native_probability: bool = True
    endpoint: str = ""
    model: str = ""

    def __post_init__(self) -> None:
        for primitive in SUPPORTED_PRIMITIVES:
            value = str(self.primitives.get(primitive, "unsupported"))
            if value not in {"native", "emulated", "unsupported"}:
                raise ValueError(f"invalid capability for {primitive}: {value}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "backend": self.backend,
            "version": self.version,
            "primitives": {name: str(self.primitives.get(name, "unsupported")) for name in SUPPORTED_PRIMITIVES},
            "native_probability": self.native_probability,
            "endpoint": self.endpoint,
            "model": self.model,
        }


@dataclass(frozen=True)
class SemanticState:
    """A bounded, shared state snapshot used by all questions in a batch."""

    state_id: str
    data: Mapping[str, Any]
    evidence_refs: Tuple[str, ...] = ()
    case_ids: Tuple[str, ...] = ()
    version: str = SEMANTIC_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state_id": self.state_id,
            "version": self.version,
            "data": dict(self.data),
            "evidence_refs": list(self.evidence_refs),
            "case_ids": list(self.case_ids),
        }

    @property
    def snapshot_hash(self) -> str:
        return stable_hash(self.to_dict())


@dataclass(frozen=True)
class SemanticQuestion:
    """One complete question with an explicit case/state path."""

    question_id: str
    axis_id: str
    prompt: str
    answer_type: str
    case_id: str = ""
    state_path: str = "state"
    choices: Tuple[str, ...] = ()
    score_levels: Tuple[str, ...] = ()
    score_min: Optional[float] = None
    score_max: Optional[float] = None
    applicability: str = "applicable"
    evidence_refs: Tuple[str, ...] = ()
    stage: int = 1
    depends_on: Tuple[str, ...] = ()
    group_version: str = SEMANTIC_VERSION
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        answer_type = self.answer_type.lower()
        if answer_type not in SUPPORTED_PRIMITIVES:
            raise ValueError(f"unsupported semantic answer type: {self.answer_type}")
        if not self.question_id.strip() or not self.axis_id.strip() or not self.prompt.strip():
            raise ValueError("question_id, axis_id, and prompt are required")
        if not self.case_id.strip():
            raise ValueError("each semantic question must identify a case")
        if self.stage <= 0:
            raise ValueError("question stage must be positive")
        if answer_type == "choice" and not self.choices:
            raise ValueError("Choice questions require a closed choice set")
        if answer_type == "choice":
            if any(not isinstance(option, str) or not option for option in self.choices):
                raise ValueError("Choice options must be non-empty strings")
            if len(set(self.choices)) != len(self.choices):
                raise ValueError("Choice options must be unique")
        if answer_type == "score" and not self.score_levels and self.score_min is None and self.score_max is None:
            raise ValueError("Score questions require ordered levels or a numeric range")
        if self.score_levels:
            if len(self.score_levels) < 2 or len(self.score_levels) > 10:
                raise ValueError("Score questions require between 2 and 10 ordered levels")
            if any(not isinstance(level, str) or not level for level in self.score_levels):
                raise ValueError("Score levels must be non-empty strings")
            if len(set(self.score_levels)) != len(self.score_levels):
                raise ValueError("Score levels must be unique")

    @property
    def type(self) -> str:
        return self.answer_type.capitalize()

    @property
    def question_type(self) -> str:
        return self.answer_type

    @property
    def primitive(self) -> str:
        return self.answer_type.lower()

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "question_id": self.question_id,
            "id": self.question_id,
            "axis_id": self.axis_id,
            "group_version": self.group_version,
            "type": self.type,
            "answer_type": self.answer_type,
            "prompt": self.prompt,
            "case_id": self.case_id,
            "state_path": self.state_path,
            "applicability": self.applicability,
            "evidence_refs": list(self.evidence_refs),
            "stage": self.stage,
            "depends_on": list(self.depends_on),
        }
        if self.choices:
            payload["choices"] = list(self.choices)
        if self.score_levels:
            payload["score_levels"] = list(self.score_levels)
        if self.score_min is not None:
            payload["score_min"] = self.score_min
        if self.score_max is not None:
            payload["score_max"] = self.score_max
        if self.metadata:
            payload["metadata"] = dict(self.metadata)
        return payload


@dataclass(frozen=True)
class SemanticAnswer:
    """A validated typed judgment returned for one question."""

    question_id: str
    primitive: str
    value: Any = None
    applicability: str = "applicable"
    status: str = "observed"
    evidence_refs: Tuple[str, ...] = ()
    rationale_ref: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def choice(self) -> Optional[str]:
        return self.value if self.primitive == "choice" and isinstance(self.value, str) else None

    @property
    def p_yes(self) -> Optional[float]:
        return self.value if self.primitive == "noul" and isinstance(self.value, (int, float)) else None

    @property
    def score(self) -> Any:
        return self.value if self.primitive == "score" else None

    @property
    def confidence(self) -> Optional[float]:
        value = self.metadata.get("confidence")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def probabilities(self) -> Optional[Dict[str, float]]:
        value = self.metadata.get("probabilities")
        return dict(value) if isinstance(value, Mapping) else None

    @property
    def legend(self) -> Optional[Dict[str, str]]:
        value = self.metadata.get("legend")
        return dict(value) if isinstance(value, Mapping) else None

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "question_id": self.question_id,
            "primitive": self.primitive,
            "value": self.value,
            "applicability": self.applicability,
            "status": self.status,
            "evidence_refs": list(self.evidence_refs),
            "rationale_ref": self.rationale_ref,
            "metadata": dict(self.metadata),
        }
        # These fields are emitted only when the native provider returned
        # them.  Mock/generic answers therefore do not acquire invented
        # confidence or distribution values.
        for name in ("confidence", "probabilities", "legend"):
            if name in self.metadata:
                payload[name] = self.metadata[name]
        return payload


@dataclass(frozen=True)
class SemanticUsage:
    """Provider-reported usage, retaining unknown values as ``None``."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None

    @classmethod
    def from_payload(cls, payload: Any) -> "SemanticUsage":
        if not isinstance(payload, Mapping):
            return cls()

        def read(*names: str) -> Optional[int]:
            for name in names:
                if name not in payload:
                    continue
                value = payload.get(name)
                if not _is_int(value) or value < 0:
                    return None
                return value
            return None

        usage = cls(
            input_tokens=read("input_tokens", "prompt_tokens", "inputTokens"),
            output_tokens=read("output_tokens", "completion_tokens", "outputTokens"),
            total_tokens=read("total_tokens", "total", "totalTokens"),
            cached_tokens=read("cached_tokens", "cache_read_tokens", "cachedTokens"),
            reasoning_tokens=read("reasoning_tokens", "reasoningTokens"),
        )
        if (
            usage.total_tokens is not None
            and usage.input_tokens is not None
            and usage.output_tokens is not None
            and usage.total_tokens < usage.input_tokens + usage.output_tokens
        ):
            usage = cls(
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                total_tokens=None,
                cached_tokens=usage.cached_tokens,
                reasoning_tokens=usage.reasoning_tokens,
            )
        return usage

    def to_dict(self) -> Dict[str, Optional[int]]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "cached_tokens": self.cached_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    @classmethod
    def sum(cls, usages: Sequence["SemanticUsage"]) -> "SemanticUsage":
        if not usages:
            return cls()

        def total(field_name: str) -> Optional[int]:
            values = [getattr(item, field_name) for item in usages]
            if any(value is None for value in values):
                return None
            return sum(int(value) for value in values)

        return cls(
            input_tokens=total("input_tokens"),
            output_tokens=total("output_tokens"),
            total_tokens=total("total_tokens"),
            cached_tokens=total("cached_tokens"),
            reasoning_tokens=total("reasoning_tokens"),
        )


@dataclass(frozen=True)
class RequestAttempt:
    request_id: str
    attempt_id: str
    attempt_number: int
    status: str
    http_status: Optional[int] = None
    retryable: bool = False
    error_kind: str = ""
    retry_after_seconds: Optional[float] = None
    elapsed_ms: Optional[int] = None
    usage: SemanticUsage = field(default_factory=SemanticUsage)
    response_hash: str = ""
    request_instance_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_id": self.request_id,
            "attempt_id": self.attempt_id,
            "attempt_number": self.attempt_number,
            "status": self.status,
            "http_status": self.http_status,
            "retryable": self.retryable,
            "error_kind": self.error_kind,
            "retry_after_seconds": self.retry_after_seconds,
            "elapsed_ms": self.elapsed_ms,
            "usage": self.usage.to_dict(),
            "response_hash": self.response_hash,
            "request_instance_id": self.request_instance_id,
        }


@dataclass
class SemanticLedger:
    """Request/attempt ledger with invocation and attempt identities."""

    attempts: List[RequestAttempt] = field(default_factory=list)

    def add(self, attempt: RequestAttempt) -> None:
        if any(existing.attempt_id == attempt.attempt_id for existing in self.attempts):
            return
        self.attempts.append(attempt)

    def extend(self, other: "SemanticLedger") -> None:
        """Merge immutable attempt records without double-counting identities."""

        for attempt in other.attempts:
            self.add(attempt)

    def snapshot(self, start: int = 0) -> "SemanticLedger":
        """Copy a bounded slice so later backend calls cannot mutate a report."""

        return SemanticLedger(attempts=list(self.attempts[max(0, start):]))

    @property
    def request_count(self) -> int:
        return len({attempt.request_instance_id or attempt.request_id for attempt in self.attempts})

    @property
    def attempt_count(self) -> int:
        return len(self.attempts)

    def usage(self) -> SemanticUsage:
        # A rejected HTTP request (401/422/429/529) can have no model usage;
        # its absent usage must not turn a later successful attempt into an
        # unknown total.  An execution whose outcome is unknown (for example a
        # timeout) is retained and therefore keeps the aggregate field null.
        usages = [
            attempt.usage
            for attempt in self.attempts
            if attempt.status not in {"failed"}
            or any(value is not None for value in attempt.usage.to_dict().values())
        ]
        return SemanticUsage.sum(usages)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "request_count": self.request_count,
            "attempt_count": self.attempt_count,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            "usage": self.usage().to_dict(),
        }


@dataclass
class SemanticResponse:
    """Backend response shared by the generic and Jev adapters."""

    status: str = "failed"
    answers: Dict[str, SemanticAnswer] = field(default_factory=dict)
    diagnostics: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    usage: SemanticUsage = field(default_factory=SemanticUsage)
    ledger: SemanticLedger = field(default_factory=SemanticLedger)
    provenance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "answers": {key: answer.to_dict() for key, answer in self.answers.items()},
            "diagnostics": self.diagnostics,
            "metadata": self.metadata,
            "usage": self.usage.to_dict(),
            "ledger": self.ledger.to_dict(),
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class TransportResult:
    status_code: int
    payload: Mapping[str, Any]
    headers: Mapping[str, str] = field(default_factory=dict)
    raw_bytes: int = 0


def _primitive_name(value: Any) -> str:
    if not isinstance(value, str):
        raise SemanticValidationError("question answer type must be a string")
    name = value.strip().lower()
    if name not in SUPPORTED_PRIMITIVES:
        raise SemanticValidationError(f"unsupported answer type: {value}")
    return name


def _answer_value(raw: Mapping[str, Any], primitive: str) -> Any:
    if primitive == "choice":
        for key in ("choice", "value", "answer"):
            if key in raw:
                return raw[key]
    if primitive == "noul":
        for key in ("noul", "value", "p_yes", "probability", "probability_yes", "pYes", "answer"):
            if key in raw:
                return raw[key]
    else:
        for key in ("score", "value", "answer", "level"):
            if key in raw:
                return raw[key]
    return None


def _native_answer_fields(raw: Mapping[str, Any], primitive: str) -> bool:
    """Return whether ``raw`` uses the native Jev answer representation."""

    native_value = {
        "choice": "choice",
        "noul": "noul",
        "score": "score",
    }[primitive]
    return native_value in raw or any(
        name in raw for name in ("probabilities", "legend", "confidence")
    )


def _validate_probability_map(
    question: SemanticQuestion,
    raw: Mapping[str, Any],
    expected_keys: Sequence[str],
) -> Dict[str, float]:
    probabilities = raw.get("probabilities")
    if not isinstance(probabilities, Mapping):
        raise SemanticValidationError(
            f"probabilities for {question.question_id} must cover every option"
        )
    expected = [str(key) for key in expected_keys]
    actual = [str(key) for key in probabilities.keys()]
    if set(actual) != set(expected) or len(actual) != len(expected):
        raise SemanticValidationError(
            f"probabilities for {question.question_id} must cover exactly {expected}"
        )
    normalized: Dict[str, float] = {}
    for key in expected:
        if key not in probabilities:
            raise SemanticValidationError(
                f"probabilities for {question.question_id} are missing {key}"
            )
        normalized[key] = _finite_number(
            probabilities[key], f"probability for {question.question_id}:{key}"
        )
        if normalized[key] < 0.0 or normalized[key] > 1.0:
            raise SemanticValidationError(
                f"probability for {question.question_id}:{key} must be in [0, 1]"
            )
    if not math.isclose(sum(normalized.values()), 1.0, rel_tol=1e-6, abs_tol=1e-6):
        raise SemanticValidationError(
            f"probabilities for {question.question_id} must sum to 1"
        )
    return normalized


def _validate_confidence(question: SemanticQuestion, raw: Mapping[str, Any]) -> float:
    if "confidence" not in raw:
        raise SemanticValidationError(f"confidence is required for {question.question_id}")
    confidence = _finite_number(raw["confidence"], f"confidence for {question.question_id}")
    if confidence < 0.0 or confidence > 1.0:
        raise SemanticValidationError(
            f"confidence for {question.question_id} must be in [0, 1]"
        )
    return confidence


def _reported_decimal_places(value: Any) -> int:
    """Return decimal places visible in a finite JSON number."""

    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return 0
    exponent = decimal_value.as_tuple().exponent
    return max(0, -int(exponent)) if isinstance(exponent, int) else 0


def _reported_rounding_error(value: Any) -> float:
    """Bound half of the last reported decimal unit; integers are exact."""

    places = _reported_decimal_places(value)
    return 0.5 * (10.0 ** -places) if places else 0.0


def _score_consistency(
    raw_score: Any,
    raw_probabilities: Mapping[Any, Any],
    probabilities: Mapping[str, float],
    level_count: int,
) -> Dict[str, Any]:
    """Return a bounded finite-precision consistency record for native Score.

    The provider's numeric fields are retained as reported.  This local policy
    treats visible decimal digits as rounded output and allows the sum of the
    score's half-unit error and each probability's weighted half-unit error;
    it is an interoperability bound, not a claim about provider internals.
    """

    weighted = sum(float(index) * probabilities[str(index)] for index in range(level_count))
    tolerance = _reported_rounding_error(raw_score)
    for index in range(level_count):
        raw_probability = raw_probabilities.get(str(index), raw_probabilities.get(index))
        tolerance += float(index) * _reported_rounding_error(raw_probability)
    tolerance += 1e-9
    numeric_score = float(raw_score)
    delta = abs(numeric_score - weighted)
    return {
        "weighted_value": weighted,
        "absolute_delta": delta,
        "tolerance": tolerance,
        "policy": SCORE_PRECISION_POLICY,
        "score_decimal_places": _reported_decimal_places(raw_score),
        "probability_decimal_places": {
            str(index): _reported_decimal_places(
                raw_probabilities.get(str(index), raw_probabilities.get(index))
            )
            for index in range(level_count)
        },
    }


def _native_metadata(
    question: SemanticQuestion,
    raw: Mapping[str, Any],
    primitive: str,
) -> Dict[str, Any]:
    """Validate and retain native provider metadata without deriving it."""

    if not _native_answer_fields(raw, primitive):
        return {}
    metadata: Dict[str, Any] = {}
    if primitive == "choice":
        probabilities = _validate_probability_map(question, raw, question.choices)
        confidence = _validate_confidence(question, raw)
        selected = raw.get("choice")
        if not isinstance(selected, str) or selected not in probabilities:
            raise SemanticValidationError(
                f"choice for {question.question_id} must be covered by probabilities"
            )
        max_probability = max(probabilities.values())
        if not math.isclose(
            probabilities[str(selected)], max_probability, rel_tol=1e-6, abs_tol=1e-6
        ):
            raise SemanticValidationError(
                f"choice for {question.question_id} must be a highest-probability option"
            )
        metadata.update({"probabilities": probabilities, "confidence": confidence})
    elif primitive == "score":
        if not question.score_levels or not 2 <= len(question.score_levels) <= 10:
            raise SemanticValidationError(
                f"native Jev Score requires 2-10 ordered levels for {question.question_id}"
            )
        legend = raw.get("legend")
        if not isinstance(legend, Mapping):
            raise SemanticValidationError(f"legend is required for {question.question_id}")
        expected_legend = {str(index): level for index, level in enumerate(question.score_levels)}
        actual_legend = {str(key): value for key, value in legend.items()}
        if actual_legend != expected_legend:
            raise SemanticValidationError(
                f"legend for {question.question_id} must match ordered score levels"
            )
        probabilities = _validate_probability_map(
            question,
            raw,
            [str(index) for index in range(len(question.score_levels))],
        )
        score = _finite_number(raw.get("score"), f"Score value for {question.question_id}")
        maximum = float(len(question.score_levels) - 1)
        if score < 0.0 or score > maximum:
            raise SemanticValidationError(
                f"Score value for {question.question_id} must be in [0, {int(maximum)}]"
            )
        consistency = _score_consistency(
            raw.get("score"),
            raw.get("probabilities", {}),
            probabilities,
            len(question.score_levels),
        )
        if consistency["absolute_delta"] > consistency["tolerance"]:
            raise SemanticValidationError(
                f"Score value for {question.question_id} must equal its probability-weighted value"
            )
        confidence = _validate_confidence(question, raw)
        metadata.update(
            {
                "legend": {key: str(value) for key, value in expected_legend.items()},
                "probabilities": probabilities,
                "confidence": confidence,
                "score_consistency": consistency,
            }
        )
    else:
        # Jev Noul has one native scalar and no distribution or confidence
        # field.  Extra probability/confidence data is not interpreted as a
        # different primitive.
        if "probabilities" in raw or "legend" in raw or "confidence" in raw:
            raise SemanticValidationError(
                f"Noul answer for {question.question_id} has unsupported native metadata"
            )
    return metadata


def validate_answer(question: SemanticQuestion, raw: Any) -> SemanticAnswer:
    """Validate one provider answer without accepting bool-as-number."""

    if not isinstance(raw, Mapping):
        raise SemanticValidationError(f"answer for {question.question_id} must be an object")
    primitive = _primitive_name(raw.get("type", raw.get("primitive", question.answer_type)))
    expected = question.answer_type.lower()
    if primitive != expected:
        raise SemanticValidationError(
            f"answer type mismatch for {question.question_id}: expected {expected}, got {primitive}"
        )
    applicability = str(raw.get("applicability", "applicable"))
    allowed_applicability = {"applicable", "not_applicable", "insufficient", "unknown", "abstained"}
    if applicability not in allowed_applicability:
        raise SemanticValidationError(f"invalid applicability for {question.question_id}")
    value = _answer_value(raw, primitive)
    if applicability in {"not_applicable", "insufficient", "unknown", "abstained"} and value is None:
        return SemanticAnswer(
            question_id=question.question_id,
            primitive=primitive,
            value=None,
            applicability=applicability,
            status="abstained",
            evidence_refs=tuple(str(item) for item in raw.get("evidence_refs", []) if isinstance(item, str)),
            rationale_ref=str(raw.get("rationale_ref", "")) if isinstance(raw.get("rationale_ref", ""), str) else "",
        )

    if primitive == "choice":
        if not isinstance(value, str):
            raise SemanticValidationError(f"Choice value for {question.question_id} must be a string")
        allowed = set(question.choices) | set(CHOICE_SPECIAL_VALUES)
        if value not in allowed:
            raise SemanticValidationError(f"Choice value outside closed set for {question.question_id}: {value}")
    elif primitive == "noul":
        value = _finite_number(value, f"Noul value for {question.question_id}")
        if value < 0.0 or value > 1.0:
            raise SemanticValidationError(f"Noul probability out of range for {question.question_id}")
    else:
        if _native_answer_fields(raw, primitive):
            value = _finite_number(value, f"Score value for {question.question_id}")
            maximum = float(len(question.score_levels) - 1)
            if value < 0.0 or value > maximum:
                raise SemanticValidationError(
                    f"Score value for {question.question_id} is outside ordered levels"
                )
        elif question.score_levels:
            if isinstance(value, bool):
                raise SemanticValidationError(f"Score value for {question.question_id} cannot be bool")
            if isinstance(value, str):
                if value not in question.score_levels:
                    raise SemanticValidationError(f"Score level outside ordered set for {question.question_id}")
            else:
                numeric = _finite_number(value, f"Score value for {question.question_id}")
                if numeric < 0 or numeric >= len(question.score_levels) or numeric != int(numeric):
                    raise SemanticValidationError(f"Score index outside ordered set for {question.question_id}")
                value = int(numeric)
        else:
            value = _finite_number(value, f"Score value for {question.question_id}")
            if question.score_min is not None and value < question.score_min:
                raise SemanticValidationError(f"Score below range for {question.question_id}")
            if question.score_max is not None and value > question.score_max:
                raise SemanticValidationError(f"Score above range for {question.question_id}")

    metadata = _native_metadata(question, raw, primitive)
    provider_metadata = raw.get("metadata", {})
    if isinstance(provider_metadata, Mapping):
        metadata["provider_metadata"] = dict(provider_metadata)
    evidence_refs = raw.get("evidence_refs", [])
    if not isinstance(evidence_refs, list) or any(not isinstance(item, str) for item in evidence_refs):
        raise SemanticValidationError(f"evidence_refs for {question.question_id} must be a string list")
    return SemanticAnswer(
        question_id=question.question_id,
        primitive=primitive,
        value=value,
        applicability=applicability,
        status="observed",
        evidence_refs=tuple(evidence_refs[:20]),
        rationale_ref=str(raw.get("rationale_ref", "")) if isinstance(raw.get("rationale_ref", ""), str) else "",
        metadata=metadata,
    )


def _extract_answers(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    raw_answers = payload.get("answers", payload.get("judgments", payload.get("results")))
    if isinstance(raw_answers, Mapping):
        items: List[Mapping[str, Any]] = []
        for question_id, raw in raw_answers.items():
            if isinstance(raw, Mapping):
                item = dict(raw)
                item.setdefault("question_id", question_id)
            else:
                item = {"question_id": question_id, "value": raw}
            items.append(item)
        return items
    if isinstance(raw_answers, list) and all(isinstance(item, Mapping) for item in raw_answers):
        return [dict(item) for item in raw_answers]
    raise SemanticValidationError("response must contain an answers list or mapping")


def validate_response(
    questions: Sequence[SemanticQuestion],
    payload: Mapping[str, Any],
) -> Tuple[Dict[str, SemanticAnswer], List[Dict[str, Any]]]:
    """Validate all returned answers and retain valid partial judgments."""

    if not isinstance(payload, Mapping):
        raise SemanticValidationError("response payload must be an object")
    by_id = {question.question_id: question for question in questions}
    raw_answers = _extract_answers(payload)
    answers: Dict[str, SemanticAnswer] = {}
    diagnostics: List[Dict[str, Any]] = []
    for index, raw in enumerate(raw_answers):
        question_id = raw.get("question_id", raw.get("id", raw.get("question")))
        if not isinstance(question_id, str) or question_id not in by_id:
            diagnostics.append({"kind": "unknown_question", "status": "failed", "index": index})
            continue
        if question_id in answers:
            diagnostics.append({"kind": "duplicate_answer", "status": "failed", "question_id": question_id})
            continue
        try:
            answers[question_id] = validate_answer(by_id[question_id], raw)
        except SemanticValidationError as exc:
            diagnostics.append({"kind": "invalid_answer", "status": "failed", "question_id": question_id, "message": str(exc)})
    for question in questions:
        if question.question_id not in answers:
            diagnostics.append({"kind": "missing_answer", "status": "unknown", "question_id": question.question_id})
    return answers, diagnostics


def _retry_after(headers: Mapping[str, str]) -> Optional[float]:
    raw = next((value for key, value in headers.items() if str(key).lower() == "retry-after"), None)
    if raw is None:
        return None
    try:
        value = float(str(raw).strip())
        return value if math.isfinite(value) and value >= 0 else None
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(str(raw))
            return max(0.0, when.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def classify_retry(status_code: Optional[int], error_kind: str = "") -> bool:
    """Return whether an attempt may be retried under the Jev contract."""

    if status_code in {401, 403, 422}:
        return False
    if status_code in {408, 429, 500, 502, 503, 529}:
        return True
    # A timeout has unknown execution state and must not be resent.
    if error_kind in {"timeout", "url_error", "invalid_response", "validation"}:
        return False
    return False


def _safe_mapping(value: Any, depth: int = 0) -> Any:
    """Bound and redact data before it can enter a remote request."""

    if depth > 8:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        output: Dict[str, Any] = {}
        sensitive = ("key", "token", "password", "secret", "authorization", "cookie")
        for raw_key, raw_value in list(value.items())[:200]:
            key = str(raw_key)
            if any(part in key.lower() for part in sensitive):
                output[key] = "[redacted]"
            else:
                output[key] = _safe_mapping(raw_value, depth + 1)
        return output
    if isinstance(value, (list, tuple)):
        return [_safe_mapping(item, depth + 1) for item in list(value)[:200]]
    if isinstance(value, str):
        if value.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith("\\\\"):
            return "[path-redacted]"
        return value[:16_000]
    if isinstance(value, (bool, int, float)) or value is None:
        if isinstance(value, float) and not math.isfinite(value):
            return None
        return value
    return str(value)[:1_000]


def _native_question_payload(question: SemanticQuestion) -> Dict[str, Any]:
    """Translate an internal question to the official Jev wire contract."""

    primitive = question.primitive
    payload: Dict[str, Any] = {
        "type": primitive,
        "instructions": question.prompt,
    }
    supplied_criteria = question.metadata.get("criteria") if isinstance(question.metadata, Mapping) else None
    if primitive == "choice":
        if isinstance(supplied_criteria, Mapping):
            criteria = {option: supplied_criteria.get(option) for option in question.choices}
        else:
            criteria = {option: None for option in question.choices}
        payload["criteria"] = criteria
    elif primitive == "noul":
        if isinstance(supplied_criteria, Mapping):
            criteria = {
                "true": supplied_criteria.get("true"),
                "false": supplied_criteria.get("false"),
            }
        else:
            criteria = {
                "true": question.prompt,
                "false": f"The evidence does not support: {question.prompt}",
            }
        payload["criteria"] = criteria
    else:
        if not 2 <= len(question.score_levels) <= 10:
            raise SemanticValidationError(
                f"native Jev Score requires 2-10 ordered levels for {question.question_id}"
            )
        payload["criteria"] = list(question.score_levels)
    return payload


def _native_questions_payload(questions: Sequence[SemanticQuestion]) -> Dict[str, Dict[str, Any]]:
    payload: Dict[str, Dict[str, Any]] = {}
    for question in questions:
        if question.question_id in payload:
            raise SemanticValidationError(f"duplicate native question id: {question.question_id}")
        payload[question.question_id] = _native_question_payload(question)
    return payload


class GenericSemanticBackend:
    """A backend facade that validates typed answers from a supplied caller.

    ``handler`` is useful for deterministic tests and local adapters.  It is
    intentionally passed only the bounded state and question dictionaries.
    """

    def __init__(
        self,
        handler: Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Mapping[str, Any]],
        *,
        capabilities: Optional[SemanticCapabilities] = None,
        model: str = "mock",
    ) -> None:
        self.handler = handler
        self.capabilities = capabilities or SemanticCapabilities(backend="generic", model=model)
        self.ledger = SemanticLedger()
        self._request_bytes = 0
        self._invocation_count = 0
        self.aggregate_budget: Optional[SemanticBudget] = None

    def evaluate(
        self,
        state: SemanticState | Mapping[str, Any],
        questions: Sequence[SemanticQuestion],
        *,
        budget: Optional[SemanticBudget] = None,
    ) -> SemanticResponse:
        budget = budget or SemanticBudget()
        if self.aggregate_budget is None:
            self.aggregate_budget = budget
        ledger_start = len(self.ledger.attempts)
        normalized_state = state.to_dict() if isinstance(state, SemanticState) else _safe_mapping(state)
        normalized_questions = [question.to_dict() for question in questions]
        if len(normalized_questions) > budget.max_questions:
            raise SemanticBudgetError("question budget exceeded")
        state_bytes = len(canonical_json(normalized_state))
        if state_bytes > budget.max_state_bytes:
            raise SemanticBudgetError("state byte budget exceeded")
        request_bytes = len(canonical_json({"state": normalized_state, "questions": normalized_questions}))
        question_bytes = len(canonical_json(normalized_questions))
        if self.ledger.request_count >= budget.max_requests:
            raise SemanticBudgetError("request budget exceeded")
        if self.ledger.attempt_count >= budget.max_attempts:
            raise SemanticBudgetError("attempt budget exceeded")
        if self._request_bytes + request_bytes > budget.max_total_bytes:
            raise SemanticBudgetError("total semantic request byte budget exceeded")
        if request_bytes > budget.max_request_bytes or any(
            len(canonical_json(question)) > budget.max_question_bytes for question in normalized_questions
        ):
            raise SemanticBudgetError("question/request byte budget exceeded")
        self._request_bytes += request_bytes
        request_id = stable_hash({"state": normalized_state, "questions": normalized_questions})
        self._invocation_count += 1
        request_instance_id = f"{request_id}:{self._invocation_count}"
        try:
            payload = self.handler(normalized_state, normalized_questions)
            if not isinstance(payload, Mapping):
                raise SemanticValidationError("generic backend returned a non-object")
            usage = SemanticUsage.from_payload(payload.get("usage"))
            try:
                answers, diagnostics = validate_response(questions, payload)
            except SemanticValidationError as exc:
                answers = {}
                diagnostics = [{"kind": "invalid_response", "status": "failed", "message": str(exc)[:300]}]
            attempt = RequestAttempt(
                request_id=request_id,
                attempt_id=f"{request_instance_id}:1",
                attempt_number=1,
                status="complete" if not diagnostics else "partial",
                usage=usage,
                response_hash=stable_hash(payload),
                request_instance_id=request_instance_id,
            )
            self.ledger.add(attempt)
            return SemanticResponse(
                status="complete" if len(answers) == len(questions) and not diagnostics else "partial",
                answers=answers,
                diagnostics=diagnostics,
                metadata={"backend": self.capabilities.backend, "model": self.capabilities.model or "mock"},
                usage=self.ledger.snapshot(ledger_start).usage(),
                ledger=self.ledger.snapshot(ledger_start),
                provenance={
                    "semantic_version": SEMANTIC_VERSION,
                    "request_id": request_id,
                    "request_instance_id": request_instance_id,
                    "state_hash": stable_hash(normalized_state),
                    "questions_hash": stable_hash(normalized_questions),
                    "usage_scope": "request_attempt_deduplicated",
                },
            )
        except SemanticError as exc:
            self.ledger.add(
                RequestAttempt(
                    request_id=request_id,
                    attempt_id=f"{request_instance_id}:1",
                    attempt_number=1,
                    status="unknown",
                    error_kind="backend_validation",
                    request_instance_id=request_instance_id,
                )
            )
            return SemanticResponse(
                status="failed",
                diagnostics=[{"kind": "backend_validation", "status": "failed", "message": str(exc)}],
                usage=self.ledger.snapshot(ledger_start).usage(),
                ledger=self.ledger.snapshot(ledger_start),
                provenance={
                    "semantic_version": SEMANTIC_VERSION,
                    "request_id": request_id,
                    "request_instance_id": request_instance_id,
                },
            )
        except Exception as exc:
            self.ledger.add(
                RequestAttempt(
                    request_id=request_id,
                    attempt_id=f"{request_instance_id}:1",
                    attempt_number=1,
                    status="unknown",
                    error_kind="backend_exception",
                    request_instance_id=request_instance_id,
                )
            )
            return SemanticResponse(
                status="failed",
                diagnostics=[{"kind": "backend_exception", "status": "failed", "message": f"{type(exc).__name__}: {exc}"[:300]}],
                usage=self.ledger.snapshot(ledger_start).usage(),
                ledger=self.ledger.snapshot(ledger_start),
                provenance={
                    "semantic_version": SEMANTIC_VERSION,
                    "request_id": request_id,
                    "request_instance_id": request_instance_id,
                },
            )


class MockSemanticBackend(GenericSemanticBackend):
    """Deterministic emulated backend for synthetic verification only."""

    def __init__(
        self,
        responses: Optional[Mapping[str, Any]] = None,
        *,
        handler: Optional[Callable[[Mapping[str, Any], Sequence[Mapping[str, Any]]], Mapping[str, Any]]] = None,
        usage: Optional[Mapping[str, Any]] = None,
    ) -> None:
        responses = dict(responses or {})
        usage_payload = dict(usage or {})

        def default_handler(state: Mapping[str, Any], questions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any]:
            answers = []
            for question in questions:
                question_id = str(question["question_id"])
                raw = responses.get(question_id)
                if isinstance(raw, Mapping):
                    item = dict(raw)
                    item.setdefault("question_id", question_id)
                elif raw is not None:
                    item = {"question_id": question_id, "type": question.get("type"), "value": raw}
                else:
                    primitive = str(question.get("answer_type", "choice")).lower()
                    if primitive == "choice":
                        value = "insufficient"
                    else:
                        value = None
                    item = {
                        "question_id": question_id,
                        "type": question.get("type"),
                        "value": value,
                        "applicability": "insufficient" if value is None or value == "insufficient" else "applicable",
                    }
                answers.append(item)
            return {"answers": answers, "usage": usage_payload}

        super().__init__(
            handler or default_handler,
            capabilities=SemanticCapabilities(
                backend="mock",
                primitives={name: "emulated" for name in SUPPORTED_PRIMITIVES},
                native_probability=False,
                model="mock",
            ),
            model="mock",
        )


class UnavailableSemanticBackend:
    """No-network backend used when Jev is not requested or has no key."""

    def __init__(self, reason: str = "TYPESAFE_API_KEY is unavailable", *, offline: bool = False) -> None:
        self.reason = reason
        self.offline = offline
        self.capabilities = SemanticCapabilities(
            backend="unavailable",
            primitives={name: "unsupported" for name in SUPPORTED_PRIMITIVES},
            native_probability=False,
        )
        self.ledger = SemanticLedger()
        self._request_bytes = 0

    def evaluate(
        self,
        state: SemanticState | Mapping[str, Any],
        questions: Sequence[SemanticQuestion],
        *,
        budget: Optional[SemanticBudget] = None,
    ) -> SemanticResponse:
        return SemanticResponse(
            status="deferred",
            diagnostics=[
                {
                    "kind": "semantic_unavailable",
                    "status": "deferred",
                    "reason": "offline" if self.offline else "missing_api_key",
                }
            ],
            metadata={"live_status": "deferred", "reason": self.reason},
            ledger=self.ledger.snapshot(),
            provenance={"semantic_version": SEMANTIC_VERSION, "usage_scope": "request_attempt_deduplicated"},
        )


class JevHTTPBackend:
    """Standard-library adapter for the Typesafe Jev HTTP endpoint."""

    def __init__(
        self,
        *,
        endpoint: str = JEV_ENDPOINT,
        model: str = DEFAULT_JEV_MODEL,
        opener: Optional[Callable[..., Any]] = None,
        sleep_fn: Optional[Callable[[float], None]] = None,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = str(model).strip() or DEFAULT_JEV_MODEL
        self._opener = opener or urlopen
        self._sleep = sleep_fn or time.sleep
        self._clock = clock or time.monotonic
        self.api_key_present = bool(os.environ.get("TYPESAFE_API_KEY"))
        self.capabilities = SemanticCapabilities(
            backend="jev",
            primitives={name: "native" for name in SUPPORTED_PRIMITIVES},
            native_probability=True,
            endpoint=endpoint,
            model=self.model,
        )
        self.ledger = SemanticLedger()
        self._request_bytes = 0
        self._invocation_count = 0
        self.aggregate_budget: Optional[SemanticBudget] = None

    def _request(self, payload: Mapping[str, Any], budget: SemanticBudget) -> TransportResult:
        key = os.environ.get("TYPESAFE_API_KEY")
        if not key:
            raise PermissionError("TYPESAFE_API_KEY is unavailable")
        encoded = canonical_json(payload)
        request = Request(
            self.endpoint,
            data=encoded,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {key}",
            },
            method="POST",
        )
        response = self._opener(request, timeout=budget.timeout_seconds)
        status_code = int(getattr(response, "status", getattr(response, "code", 200)))
        response_headers = getattr(response, "headers", None)
        headers = (
            {str(header_key): str(header_value) for header_key, header_value in response_headers.items()}
            if response_headers is not None
            else {}
        )
        body = response.read(budget.max_response_bytes + 1)
        if not isinstance(body, bytes):
            body = str(body).encode("utf-8", "replace")
        if len(body) > budget.max_response_bytes:
            raise SemanticResponseError(
                "semantic response byte budget exceeded",
                error_kind="response_budget",
                status_code=status_code,
                response_hash=hashlib.sha256(body[:4096]).hexdigest(),
            )
        text = body.decode("utf-8", "replace")
        try:
            decoded = json.loads(text) if text else {}
        except (json.JSONDecodeError, ValueError) as exc:
            if 200 <= status_code < 300:
                raise SemanticResponseError(
                    f"semantic response is not JSON: {exc}",
                    status_code=status_code,
                    response_hash=hashlib.sha256(body).hexdigest(),
                ) from exc
            decoded = {}
        if not isinstance(decoded, Mapping):
            if 200 <= status_code < 300:
                raise SemanticResponseError(
                    "semantic response JSON must be an object",
                    status_code=status_code,
                    response_hash=hashlib.sha256(body).hexdigest(),
                )
            decoded = {}
        return TransportResult(status_code=status_code, payload=decoded, headers=headers, raw_bytes=len(body))

    def evaluate(
        self,
        state: SemanticState | Mapping[str, Any],
        questions: Sequence[SemanticQuestion],
        *,
        budget: Optional[SemanticBudget] = None,
    ) -> SemanticResponse:
        budget = budget or SemanticBudget()
        if self.aggregate_budget is None:
            self.aggregate_budget = budget
        ledger_start = len(self.ledger.attempts)
        normalized_state = state.to_dict() if isinstance(state, SemanticState) else _safe_mapping(state)
        if len(questions) > budget.max_questions:
            raise SemanticBudgetError("question budget exceeded")
        state_bytes = len(canonical_json(normalized_state))
        if state_bytes > budget.max_state_bytes:
            raise SemanticBudgetError("state byte budget exceeded")
        question_payload = _native_questions_payload(questions)
        question_bytes = len(canonical_json(question_payload))
        if any(
            len(canonical_json(question)) > budget.max_question_bytes
            for question in question_payload.values()
        ):
            raise SemanticBudgetError("single question byte budget exceeded")
        payload: Dict[str, Any] = {
            "model": self.model,
            "state": normalized_state,
            "questions": question_payload,
        }
        request_bytes = len(canonical_json(payload))
        if request_bytes > budget.max_request_bytes:
            raise SemanticBudgetError("semantic request byte budget exceeded")
        if self._request_bytes + request_bytes > budget.max_total_bytes:
            raise SemanticBudgetError("total semantic request byte budget exceeded")
        request_id = stable_hash(payload)
        if self.ledger.request_count >= budget.max_requests:
            raise SemanticBudgetError("request budget exceeded")
        if self.ledger.attempt_count >= budget.max_attempts:
            raise SemanticBudgetError("attempt budget exceeded")
        self._request_bytes += request_bytes

        if not os.environ.get("TYPESAFE_API_KEY"):
            return SemanticResponse(
                status="deferred",
                diagnostics=[{"kind": "missing_api_key", "status": "deferred"}],
                metadata={"live_status": "deferred"},
                ledger=self.ledger.snapshot(ledger_start),
                provenance={
                    "semantic_version": SEMANTIC_VERSION,
                    "request_id": request_id,
                    "requested_model": self.model,
                    "state_hash": stable_hash(normalized_state),
                    "questions_hash": stable_hash(question_payload),
                    "request_bytes": request_bytes,
                    "state_bytes": state_bytes,
                    "question_bytes": question_bytes,
                    "usage_scope": "request_attempt_deduplicated",
                },
            )

        self._invocation_count += 1
        request_instance_id = f"{request_id}:{self._invocation_count}"
        diagnostics: List[Dict[str, Any]] = []
        max_attempts = min(budget.max_retries + 1, budget.max_attempts - self.ledger.attempt_count)
        for attempt_number in range(1, max_attempts + 1):
            attempt_id = f"{request_instance_id}:{attempt_number}"
            started = self._clock()
            retryable = False
            try:
                result = self._request(payload, budget)
                response_payload = result.payload
                usage = SemanticUsage.from_payload(response_payload.get("usage"))
                response_hash = stable_hash(response_payload)
                status_code = result.status_code
                if 200 <= status_code < 300:
                    try:
                        answers, validation = validate_response(questions, response_payload)
                    except SemanticValidationError as exc:
                        answers = {}
                        validation = [{"kind": "invalid_response", "status": "failed", "message": str(exc)}]
                    diagnostics.extend(validation)
                    attempt_status = "complete" if not validation else "partial"
                    self.ledger.add(
                        RequestAttempt(
                            request_id=request_id,
                            attempt_id=attempt_id,
                            attempt_number=attempt_number,
                            status=attempt_status,
                            http_status=status_code,
                            usage=usage,
                            elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                            response_hash=response_hash,
                            request_instance_id=request_instance_id,
                        )
                    )
                    return SemanticResponse(
                        status="complete" if len(answers) == len(questions) and not validation else "partial",
                        answers=answers,
                        diagnostics=diagnostics,
                        metadata={"live_status": "complete", "http_status": status_code, "model": response_payload.get("model", self.model)},
                        usage=self.ledger.snapshot(ledger_start).usage(),
                        ledger=self.ledger.snapshot(ledger_start),
                        provenance={
                            "semantic_version": SEMANTIC_VERSION,
                            "request_id": request_id,
                            "request_instance_id": request_instance_id,
                            "requested_model": self.model,
                            "state_hash": stable_hash(normalized_state),
                            "questions_hash": stable_hash(question_payload),
                            "request_bytes": request_bytes,
                            "state_bytes": state_bytes,
                            "question_bytes": question_bytes,
                            "usage_scope": "request_attempt_deduplicated",
                        },
                    )
                retryable = classify_retry(status_code)
                retry_after = _retry_after(result.headers)
                diagnostics.append({"kind": "http_error", "status": "failed", "http_status": status_code})
                self.ledger.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        status="failed",
                        http_status=status_code,
                        retryable=retryable,
                        error_kind="http",
                        retry_after_seconds=retry_after,
                        elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                        usage=usage,
                        response_hash=response_hash,
                        request_instance_id=request_instance_id,
                    )
                )
                if retryable and attempt_number < max_attempts:
                    if retry_after is not None and retry_after > budget.retry_after_cap_seconds:
                        diagnostics.append(
                            {
                                "kind": "retry_deferred",
                                "status": "deferred",
                                "http_status": status_code,
                                "retry_after_seconds": retry_after,
                                "local_wait_cap_seconds": budget.retry_after_cap_seconds,
                            }
                        )
                        return SemanticResponse(
                            status="deferred",
                            diagnostics=diagnostics,
                            metadata={"live_status": "deferred", "http_status": status_code},
                            usage=self.ledger.snapshot(ledger_start).usage(),
                            ledger=self.ledger.snapshot(ledger_start),
                            provenance={
                                "semantic_version": SEMANTIC_VERSION,
                                "request_id": request_id,
                                "request_instance_id": request_instance_id,
                                "requested_model": self.model,
                                "usage_scope": "request_attempt_deduplicated",
                            },
                        )
                    delay = retry_after if retry_after is not None else min(2.0 ** (attempt_number - 1), budget.retry_after_cap_seconds)
                    delay = min(max(0.0, delay), budget.retry_after_cap_seconds)
                    self._sleep(delay)
                    continue
                return SemanticResponse(
                    status="failed",
                    diagnostics=diagnostics,
                    metadata={"live_status": "failed", "http_status": status_code},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
            except HTTPError as exc:
                status_code = int(exc.code)
                retryable = classify_retry(status_code)
                headers = {str(key): str(value) for key, value in exc.headers.items()} if exc.headers is not None else {}
                retry_after = _retry_after(headers)
                body = exc.read(budget.max_response_bytes + 1)
                response_hash = stable_hash({"status": status_code, "body": body[:4096].decode("utf-8", "replace")})
                usage = SemanticUsage()
                if body:
                    try:
                        error_payload = json.loads(body.decode("utf-8", "replace"))
                        if isinstance(error_payload, Mapping):
                            usage = SemanticUsage.from_payload(error_payload.get("usage"))
                    except (ValueError, UnicodeDecodeError):
                        pass
                self.ledger.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        status="failed",
                        http_status=status_code,
                        retryable=retryable,
                        error_kind="http",
                        retry_after_seconds=retry_after,
                        elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                        usage=usage,
                        response_hash=response_hash,
                        request_instance_id=request_instance_id,
                    )
                )
                diagnostics.append({"kind": "http_error", "status": "failed", "http_status": status_code})
                if retryable and attempt_number < max_attempts:
                    if retry_after is not None and retry_after > budget.retry_after_cap_seconds:
                        diagnostics.append(
                            {
                                "kind": "retry_deferred",
                                "status": "deferred",
                                "http_status": status_code,
                                "retry_after_seconds": retry_after,
                                "local_wait_cap_seconds": budget.retry_after_cap_seconds,
                            }
                        )
                        return SemanticResponse(
                            status="deferred",
                            diagnostics=diagnostics,
                            metadata={"live_status": "deferred", "http_status": status_code},
                            usage=self.ledger.snapshot(ledger_start).usage(),
                            ledger=self.ledger.snapshot(ledger_start),
                            provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                        )
                    delay = retry_after if retry_after is not None else min(2.0 ** (attempt_number - 1), budget.retry_after_cap_seconds)
                    self._sleep(min(max(0.0, delay), budget.retry_after_cap_seconds))
                    continue
                return SemanticResponse(
                    status="failed",
                    diagnostics=diagnostics,
                    metadata={"live_status": "failed", "http_status": status_code},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
            except SemanticResponseError as exc:
                self.ledger.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        status="unknown",
                        http_status=exc.status_code,
                        retryable=False,
                        error_kind=exc.error_kind,
                        elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                        response_hash=exc.response_hash,
                        request_instance_id=request_instance_id,
                    )
                )
                diagnostics.append(
                    {
                        "kind": "invalid_response",
                        "status": "unknown",
                        "error_kind": exc.error_kind,
                        "http_status": exc.status_code,
                        "message": str(exc)[:300],
                    }
                )
                return SemanticResponse(
                    status="unknown",
                    diagnostics=diagnostics,
                    metadata={"live_status": "unknown"},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
            except TimeoutError:
                # Timeout is deliberately classified as unknown: the provider
                # may have executed the request, so it is never retried.
                self.ledger.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        status="unknown",
                        retryable=False,
                        error_kind="timeout",
                        elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                        request_instance_id=request_instance_id,
                    )
                )
                return SemanticResponse(
                    status="unknown",
                    diagnostics=[{"kind": "timeout", "status": "unknown", "retry": "not_retried"}],
                    metadata={"live_status": "unknown"},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
            except URLError as exc:
                self.ledger.add(
                    RequestAttempt(
                        request_id=request_id,
                        attempt_id=attempt_id,
                        attempt_number=attempt_number,
                        status="failed",
                        retryable=False,
                        error_kind="url_error",
                        elapsed_ms=max(0, int((self._clock() - started) * 1000)),
                        request_instance_id=request_instance_id,
                    )
                )
                return SemanticResponse(
                    status="failed",
                    diagnostics=[{"kind": "transport_error", "status": "failed", "message": str(exc)[:200]}],
                    metadata={"live_status": "failed"},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
            except (SemanticBudgetError, SemanticValidationError, PermissionError) as exc:
                return SemanticResponse(
                    status="failed" if not isinstance(exc, PermissionError) else "deferred",
                    diagnostics=[{"kind": "request_rejected", "status": "failed", "message": str(exc)}],
                    metadata={"live_status": "deferred" if isinstance(exc, PermissionError) else "failed"},
                    usage=self.ledger.snapshot(ledger_start).usage(),
                    ledger=self.ledger.snapshot(ledger_start),
                    provenance={"semantic_version": SEMANTIC_VERSION, "request_id": request_id, "request_instance_id": request_instance_id, "requested_model": self.model, "usage_scope": "request_attempt_deduplicated"},
                )
        return SemanticResponse(
            status="failed",
            diagnostics=diagnostics,
            usage=self.ledger.snapshot(ledger_start).usage(),
            ledger=self.ledger.snapshot(ledger_start),
        )


# Compatibility aliases used by callers that spell the adapter differently.
JevHttpBackend = JevHTTPBackend
JevBackend = JevHTTPBackend
HTTPJevBackend = JevHTTPBackend
SemanticBackend = GenericSemanticBackend
SemanticLimits = SemanticBudget


def build_default_backend(*, offline: bool = False, endpoint: Optional[str] = None, model: str = "") -> Any:
    """Build a backend without ever placing credentials in an artifact."""

    if offline:
        return UnavailableSemanticBackend("semantic evaluation disabled by offline mode", offline=True)
    if not os.environ.get("TYPESAFE_API_KEY"):
        return UnavailableSemanticBackend()
    return JevHTTPBackend(
        endpoint=endpoint or JEV_ENDPOINT,
        model=str(model).strip() or DEFAULT_JEV_MODEL,
    )
