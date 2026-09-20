"""Regress the observed routing abstention and preserve the decision evidence."""
import json
from types import SimpleNamespace

import pytest

from lib.agent_analysis import AgentConfig
from lib.jev_routing import AnalysisRequest, choose_model
from lib.semantic_backend import JevHTTPBackend, SemanticBudget


def candidate(name, priority=1, status="available"):
    return AgentConfig(
        name, lambda _: ["fixture"], executor="fixture", provider="fixture",
        route="fixture.stdin", model_id=name, priority=priority,
        availability={"status": status, "expires_at": "2099-01-01T00:00:00Z"},
    )


def evaluate(monkeypatch, candidates, choice, probabilities, confidence=0.2):
    sent = []

    def opener(request, timeout):
        payload = json.loads(request.data)
        sent.append(payload)
        qid = next(iter(payload["questions"]))
        result = {"model": "jev-fixture-served", "answers": {qid: {
            "type": "choice", "choice": choice, "probabilities": probabilities,
            "confidence": confidence,
        }}, "usage": {"input_tokens": 1355, "output_tokens": 129}}
        return SimpleNamespace(status=200, headers={}, read=lambda _: json.dumps(result).encode())

    monkeypatch.setenv("TYPESAFE_API_KEY", "test-fixture")
    decision = choose_model(candidates, AnalysisRequest(
        task_profile={"deliverable": "Analyze 20 session summaries in Traditional Chinese",
                      "requirements": ["Cite evidence; preserve unknown values"]},
    ), backend=JevHTTPBackend(model="jev-latest", opener=opener), budget=SemanticBudget(max_retries=0))
    return decision, sent[0]


def test_task_and_defined_options_reach_native_wire_and_receipt(monkeypatch):
    a = candidate("analyst")
    probabilities = {a.candidate_id: 0.6, "no_suitable_model": 0.1, "insufficient_model_evidence": 0.3}
    decision, wire = evaluate(monkeypatch, [a], a.candidate_id, probabilities)
    assert wire["state"]["data"]["routing"]["task_profile"]["requirements"] == ["Cite evidence; preserve unknown values"]
    criteria = next(iter(wire["questions"].values()))["criteria"]
    assert all(value is not None for value in criteria.values())
    assert criteria["no_suitable_model"] != criteria["insufficient_model_evidence"]
    record = decision.to_dict()
    assert record["status"] == "selected"  # Low confidence is not automatic abstention.
    assert record["jev_probabilities"] == probabilities
    assert record["jev_confidence"] == 0.2
    assert record["jev_actual_model"] == "jev-fixture-served"
    assert record["jev_requested_model"] == "jev-latest"
    assert len(record["jev_request_id"]) == len(record["jev_response_hash"]) == 64
    assert record["jev_usage"]["input_tokens"] == 1355
    assert record["jev_usage"]["total_tokens"] is None


@pytest.mark.parametrize("reason", ["no_suitable_model", "insufficient_model_evidence"])
def test_explicit_abstention_retains_reason_without_silent_fallback(monkeypatch, reason):
    a = candidate("analyst")
    probabilities = {a.candidate_id: 0.1, "no_suitable_model": 0.1, "insufficient_model_evidence": 0.1}
    probabilities[reason] = 0.8
    decision, _ = evaluate(monkeypatch, [a], reason, probabilities)
    assert decision.status == "no_suitable_model"  # Legacy report status remains compatible.
    assert decision.candidate is None
    assert decision.abstention_reason == reason
    assert decision.choice_value == reason
    assert decision.jev_probabilities[reason] == 0.8


def test_exact_candidate_tie_uses_priority_and_retains_native_choice(monkeypatch):
    a, b = candidate("A", 1), candidate("B", 2)
    probabilities = {a.candidate_id: 0.5, b.candidate_id: 0.5,
                     "no_suitable_model": 0.0, "insufficient_model_evidence": 0.0}
    decision, _ = evaluate(monkeypatch, [b, a], b.candidate_id, probabilities, confidence=0.0)
    assert decision.candidate_id == a.candidate_id
    assert decision.choice_value == b.candidate_id
    assert decision.routing_source == "deterministic_tiebreak"


def test_discovered_unknown_candidate_is_not_offered_to_jev(monkeypatch):
    ready, unknown = candidate("ready"), candidate("new", status="unknown")
    probabilities = {ready.candidate_id: 0.8, "no_suitable_model": 0.1, "insufficient_model_evidence": 0.1}
    decision, wire = evaluate(monkeypatch, [unknown, ready], ready.candidate_id, probabilities)
    assert [c["candidate_id"] for c in wire["state"]["data"]["routing"]["candidates"]] == [ready.candidate_id]
    assert any("availability_unknown" in item.reasons for item in decision.eligibility)


def test_profile_affects_request_identity_and_is_bounded_and_redacted():
    a = AnalysisRequest(task_profile={"deliverable": "single session"})
    b = AnalysisRequest(task_profile={"deliverable": "batch analysis"})
    assert a.request_id != b.request_id
    private = AnalysisRequest(task_profile={"api_key": "not-a-real-key", "file": "/private/example"})
    assert private.to_dict()["task_profile"] == {"api_key": "[redacted]", "file": "[path-redacted]"}
    with pytest.raises(ValueError, match="task_profile"):
        AnalysisRequest(task_profile={"first": "a" * 10000, "second": "b" * 10000})
