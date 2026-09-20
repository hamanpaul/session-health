"""Regression coverage for the bounded typed semantic slice."""

from __future__ import annotations

from pathlib import Path
import json
import os
import unittest
from unittest.mock import patch

from lib.bundle import build_session_bundle
from lib.jev_analysis import evaluate_session_semantic
from lib.jev_questions import AXIS_IDS, build_semantic_cases, build_semantic_questions, build_semantic_state
from lib.parser_base import Session, Turn
from lib.semantic_backend import (
    CHOICE_SPECIAL_VALUES,
    JevHTTPBackend,
    MockSemanticBackend,
    SemanticBudget,
    SemanticQuestion,
    SemanticState,
    SemanticValidationError,
    classify_retry,
    validate_answer,
)


class _Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


class JevSemanticRegressionTest(unittest.TestCase):
    def _session(self):
        return Session(
            id="semantic-fixture",
            source="codex",
            model="fixture-model",
            turns=[
                Turn(
                    index=1,
                    user_input="Verify the recorded state.",
                    assistant_output="I checked the state and recorded the result.",
                ),
                Turn(
                    index=2,
                    user_input="Now preserve the result.",
                    assistant_output="The result remains available.",
                ),
            ],
        )

    def test_questions_cover_all_axes_and_share_one_state(self):
        session = self._session()
        bundle = build_session_bundle(session)
        cases = build_semantic_cases(session, bundle)
        state = build_semantic_state(session, bundle, cases)
        questions = build_semantic_questions(cases)

        self.assertEqual(set(question.axis_id for question in questions), set(AXIS_IDS))
        self.assertEqual(len(state.case_ids), len(cases))
        self.assertTrue(all(question.state_path.startswith("state.data.cases.") for question in questions))
        self.assertEqual(set(question.stage for question in questions), {1, 2})
        self.assertEqual(len({question.question_id for question in questions}), len(questions))
        self.assertIn("noul", {question.answer_type for question in questions})
        self.assertIn("score", {question.answer_type for question in questions})
        self.assertIn("choice", {question.answer_type for question in questions})

    def test_typed_validation_rejects_bool_nan_and_out_of_range_values(self):
        noul = SemanticQuestion(
            question_id="state",
            axis_id="STATE",
            prompt="state sufficiency",
            answer_type="noul",
            case_id="case-1",
        )
        for value in (True, -0.01, 1.01, float("nan")):
            with self.assertRaises(SemanticValidationError):
                validate_answer(noul, {"question_id": "state", "type": "Noul", "value": value})

        choice = SemanticQuestion(
            question_id="choice",
            axis_id="SNR",
            prompt="relevance",
            answer_type="choice",
            choices=("relevant",),
            case_id="case-1",
        )
        self.assertIn("none", CHOICE_SPECIAL_VALUES)
        self.assertEqual(
            validate_answer(choice, {"question_id": "choice", "value": "none"}).value,
            "none",
        )
        with self.assertRaises(SemanticValidationError):
            validate_answer(choice, {"question_id": "choice", "value": "made-up"})

    def test_mock_pipeline_runs_dependent_stage_and_accounts_usage_per_request(self):
        session = self._session()
        bundle = build_session_bundle(session)
        cases = build_semantic_cases(session, bundle, max_cases=2)
        questions = build_semantic_questions(cases)
        responses = {}
        for question in questions:
            if question.stage == 2:
                responses[question.question_id] = "supported"
            elif question.answer_type == "choice":
                responses[question.question_id] = question.choices[0]
            elif question.answer_type == "noul":
                responses[question.question_id] = 0.8
            else:
                responses[question.question_id] = question.score_levels[-1]

        result = evaluate_session_semantic(
            session,
            bundle=bundle,
            backend=MockSemanticBackend(responses, usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}),
            budget=SemanticBudget(max_requests=8, max_attempts=12),
        )

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.live_status, "mock")
        self.assertEqual(len(result.stages), 2)
        # Two requests (stage 1 and stage 2), not one usage record per case or
        # per answer.  Both attempts report complete usage.
        self.assertEqual(result.ledger["request_count"], 2)
        self.assertEqual(result.usage["total_tokens"], 30)
        self.assertEqual(result.coverage["evaluated_case_count"], 2)

    def test_native_jev_wire_and_native_answers_preserve_typed_metadata(self):
        questions = [
            SemanticQuestion(
                question_id="n0",
                axis_id="STATE",
                prompt="Is the state sufficient?",
                answer_type="noul",
                case_id="case-1",
            ),
            SemanticQuestion(
                question_id="c0",
                axis_id="SNR",
                prompt="Classify the evidence.",
                answer_type="choice",
                choices=("relevant", "irrelevant"),
                case_id="case-1",
            ),
            SemanticQuestion(
                question_id="s0",
                axis_id="CTX",
                prompt="Score continuity.",
                answer_type="score",
                score_levels=("lost", "partial", "continuous"),
                case_id="case-1",
            ),
        ]
        response = _Response(
            {
                "model": "jev-1.13.0",
                "answers": {
                    "n0": {"type": "noul", "noul": 0.92},
                    "c0": {
                        "type": "choice",
                        "choice": "relevant",
                        "probabilities": {"relevant": 0.8, "irrelevant": 0.2},
                        "confidence": 0.61,
                    },
                    "s0": {
                        "type": "score",
                        "score": 1.6,
                        "legend": {"0": "lost", "1": "partial", "2": "continuous"},
                        "probabilities": {"0": 0.05, "1": 0.3, "2": 0.65},
                        "confidence": 0.78,
                    },
                },
                "usage": {"input_tokens": 409, "output_tokens": 22},
            }
        )
        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return response

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            result = JevHTTPBackend(model="", opener=opener).evaluate(
                SemanticState(state_id="state-1", data={"cases": {"case-1": {"text": "bounded"}}}),
                questions,
                budget=SemanticBudget(max_requests=1, max_attempts=1, max_retries=0),
            )

        self.assertEqual(result.status, "complete")
        self.assertEqual(set(sent[0]), {"model", "state", "questions"})
        self.assertEqual(sent[0]["model"], "jev-latest")
        self.assertIsInstance(sent[0]["questions"], dict)
        self.assertEqual(sent[0]["questions"]["n0"]["type"], "noul")
        self.assertEqual(sent[0]["questions"]["c0"]["type"], "choice")
        self.assertEqual(sent[0]["questions"]["s0"]["type"], "score")
        self.assertIn("instructions", sent[0]["questions"]["n0"])
        self.assertEqual(sent[0]["questions"]["s0"]["criteria"], ["lost", "partial", "continuous"])
        self.assertEqual(result.answers["n0"].value, 0.92)
        self.assertEqual(result.answers["c0"].probabilities, {"relevant": 0.8, "irrelevant": 0.2})
        self.assertEqual(result.answers["c0"].confidence, 0.61)
        self.assertEqual(result.answers["s0"].value, 1.6)
        self.assertEqual(result.answers["s0"].legend["2"], "continuous")

    def test_native_score_preserves_recorded_finite_precision_response(self):
        question = SemanticQuestion(
            question_id="score-precision",
            axis_id="CONV",
            prompt="Evaluate the recorded delivery claim.",
            answer_type="score",
            score_levels=("unsupported", "partially_supported", "supported"),
            case_id="case-1",
        )
        raw = {
            "question_id": question.question_id,
            "type": "score",
            "score": 0.07,
            "confidence": 0.89,
            "legend": {
                "0": "unsupported",
                "1": "partially_supported",
                "2": "supported",
            },
            "probabilities": {"0": 0.97, "1": 0.0, "2": 0.03},
        }

        answer = validate_answer(question, raw)

        self.assertEqual(answer.value, 0.07)
        self.assertEqual(answer.probabilities, {"0": 0.97, "1": 0.0, "2": 0.03})
        self.assertAlmostEqual(answer.metadata["score_consistency"]["weighted_value"], 0.06)
        self.assertAlmostEqual(answer.metadata["score_consistency"]["absolute_delta"], 0.01)
        self.assertGreater(answer.metadata["score_consistency"]["tolerance"], 0.01)

        with self.assertRaises(SemanticValidationError):
            validate_answer(
                question,
                {
                    **raw,
                    "score": 0.90,
                    "probabilities": {"0": 1.0, "1": 0.0, "2": 0.0},
                },
            )

    def test_identical_wire_calls_count_independent_requests_and_invalid_json_counts_attempt(self):
        question = SemanticQuestion(
            question_id="q",
            axis_id="STATE",
            prompt="state sufficiency",
            answer_type="noul",
            case_id="case-1",
        )
        calls = []

        def opener(request, timeout):
            calls.append(request.data)
            return _Response({"answers": {"q": {"type": "noul", "noul": 0.75}}, "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}})

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            backend = JevHTTPBackend(opener=opener)
            first = backend.evaluate({"state_id": "s", "data": {"bounded": True}}, [question], budget=SemanticBudget(max_requests=2, max_attempts=2, max_retries=0))
            second = backend.evaluate({"state_id": "s", "data": {"bounded": True}}, [question], budget=SemanticBudget(max_requests=2, max_attempts=2, max_retries=0))

        self.assertEqual(len(calls), 2)
        self.assertEqual(first.ledger.request_count, 1)
        self.assertEqual(first.ledger.attempt_count, 1)
        self.assertEqual(first.usage.to_dict()["total_tokens"], 6)
        self.assertEqual(second.ledger.request_count, 1)
        self.assertEqual(second.ledger.attempt_count, 1)
        self.assertEqual(second.usage.to_dict()["total_tokens"], 6)
        self.assertEqual(backend.ledger.request_count, 2)
        self.assertEqual(backend.ledger.attempt_count, 2)
        self.assertEqual(backend.ledger.usage().to_dict()["total_tokens"], 12)
        self.assertNotEqual(first.ledger.attempts[0].attempt_id, second.ledger.attempts[0].attempt_id)

    def test_reused_backend_scopes_two_session_reports_and_keeps_aggregate_caps(self):
        question = SemanticQuestion(
            question_id="q",
            axis_id="STATE",
            prompt="state sufficiency",
            answer_type="noul",
            case_id="case-1",
        )

        class SessionResponse:
            status = 200
            headers = {}

            def __init__(self, state_id):
                self.state_id = state_id

            def read(self, limit=-1):
                return json.dumps(
                    {
                        "model": "jev-1.13.0",
                        "answers": {"q": {"type": "noul", "noul": 0.75}},
                        "usage": {"input_tokens": len(self.state_id), "output_tokens": 2, "total_tokens": len(self.state_id) + 2},
                    }
                ).encode("utf-8")

        states = []

        def opener(request, timeout):
            payload = json.loads(request.data)
            state_id = payload["state"]["state_id"]
            states.append(state_id)
            return SessionResponse(state_id)

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            backend = JevHTTPBackend(opener=opener)
            first = backend.evaluate(
                {"state_id": "session-a", "data": {}},
                [question],
                budget=SemanticBudget(max_requests=4, max_attempts=4, max_retries=0),
            )
            second = backend.evaluate(
                {"state_id": "session-b", "data": {}},
                [question],
                budget=SemanticBudget(max_requests=4, max_attempts=4, max_retries=0),
            )

        self.assertEqual(states, ["session-a", "session-b"])
        self.assertNotEqual(first.ledger.attempts[0].request_id, second.ledger.attempts[0].request_id)
        self.assertEqual(first.usage.to_dict()["total_tokens"], len("session-a") + 2)
        self.assertEqual(second.usage.to_dict()["total_tokens"], len("session-b") + 2)
        self.assertEqual(first.ledger.attempt_count, 1)
        self.assertEqual(second.ledger.attempt_count, 1)
        self.assertEqual(backend.ledger.attempt_count, 2)
        self.assertEqual(backend.ledger.usage().to_dict()["total_tokens"], len("session-a") + len("session-b") + 4)

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            invalid = JevHTTPBackend(opener=lambda request, timeout: _Response(b"not-json")).evaluate(
                {"state_id": "s", "data": {"bounded": True}},
                [question],
                budget=SemanticBudget(max_requests=1, max_attempts=1, max_retries=0),
            )
        self.assertEqual(invalid.status, "unknown")
        self.assertEqual(invalid.ledger.attempt_count, 1)
        self.assertEqual(invalid.ledger.attempts[0].error_kind, "invalid_response")

    def test_retry_after_beyond_local_cap_defers_without_shortened_retry(self):
        question = SemanticQuestion(
            question_id="q",
            axis_id="STATE",
            prompt="state sufficiency",
            answer_type="noul",
            case_id="case-1",
        )
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(1)
            response = _Response({"error": "busy"})
            response.status = 429
            response.headers = {"Retry-After": "120"}
            return response

        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            result = JevHTTPBackend(opener=opener, sleep_fn=sleeps.append).evaluate(
                {"state_id": "s", "data": {"bounded": True}},
                [question],
                budget=SemanticBudget(max_requests=1, max_attempts=2, max_retries=1),
            )
        self.assertEqual(result.status, "deferred")
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])
        self.assertEqual(result.ledger.attempt_count, 1)

    def test_stage_two_retains_judgments_and_sampling_is_explicit(self):
        session = Session(
            id="sampling-fixture",
            source="codex",
            turns=[
                Turn(index=1, user_input="one", assistant_output="one"),
                Turn(index=2, user_input="two", assistant_output="two"),
                Turn(index=3, user_input="three", assistant_output="three"),
            ],
        )
        bundle = build_session_bundle(session)
        calls = []

        def handler(state, questions):
            calls.append(state)
            answers = {}
            for question in questions:
                primitive = question["answer_type"]
                if primitive == "choice":
                    value = question["choices"][0]
                elif primitive == "noul":
                    value = 0.8
                else:
                    value = question["score_levels"][-1]
                answers[question["question_id"]] = {
                    "question_id": question["question_id"],
                    "type": question["type"],
                    "value": value,
                }
            return {"answers": answers}

        result = evaluate_session_semantic(
            session,
            bundle=bundle,
            backend=MockSemanticBackend(handler=handler),
            budget=SemanticBudget(max_cases=1, max_requests=8, max_attempts=8),
        )

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.coverage["source_case_count"], 3)
        self.assertEqual(result.coverage["selected_case_count"], 1)
        self.assertEqual(result.coverage["excluded_case_count"], 2)
        self.assertEqual(result.coverage["sampling_coverage"], 1 / 3)
        self.assertLess(result.coverage["semantic_coverage"], 1.0)
        self.assertTrue(any(item.get("kind") == "case_sampling" for item in result.diagnostics))
        self.assertEqual(result.state["source_bundle"]["artifact_id"], bundle.manifest["artifact_id"])
        self.assertEqual(result.state["offline_coverage"], bundle.coverage)
        self.assertIn("stage1_judgments", calls[1]["data"])
        self.assertTrue(calls[1]["data"]["stage1_judgments"])

    def test_shared_semantic_backend_keeps_session_ledgers_independent(self):
        def handler(state, questions):
            answers = {}
            for question in questions:
                if question["answer_type"] == "choice":
                    value = question["choices"][0]
                elif question["answer_type"] == "noul":
                    value = 0.8
                else:
                    value = question["score_levels"][-1]
                answers[question["question_id"]] = {
                    "question_id": question["question_id"],
                    "type": question["type"],
                    "value": value,
                }
            return {"answers": answers, "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}}

        backend = MockSemanticBackend(handler=handler)
        budget = SemanticBudget(max_requests=8, max_attempts=8)
        first = evaluate_session_semantic(self._session(), backend=backend, budget=budget)
        second_session = Session(
            id="semantic-fixture-second",
            source="codex",
            turns=[Turn(index=1, user_input="second", assistant_output="second")],
        )
        second = evaluate_session_semantic(second_session, backend=backend, budget=budget)

        self.assertEqual(first.ledger["attempt_count"], 2)
        self.assertEqual(second.ledger["attempt_count"], 2)
        self.assertEqual(first.usage["total_tokens"], 30)
        self.assertEqual(second.usage["total_tokens"], 30)
        self.assertEqual(backend.ledger.attempt_count, 4)
        self.assertEqual(backend.ledger.usage().to_dict()["total_tokens"], 60)
        self.assertEqual(first.provenance["ledger_scope"], "single_session_evaluation")
        self.assertEqual(first.provenance["budget_scope"], "backend_instance_aggregate")

    def test_429_retries_with_retry_after_but_401_and_timeout_do_not(self):
        question = SemanticQuestion(
            question_id="q",
            axis_id="STATE",
            prompt="state sufficiency",
            answer_type="noul",
            case_id="case-1",
        )
        calls = []

        def opener(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                from urllib.error import HTTPError

                error = HTTPError(request.full_url, 429, "busy", {"Retry-After": "0"}, None)
                raise error
            return _Response({"answers": [{"question_id": "q", "type": "Noul", "value": 0.75}], "usage": {"total_tokens": 4}})

        slept = []
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            backend = JevHTTPBackend(opener=opener, sleep_fn=slept.append)
            result = backend.evaluate(
                {"state_id": "s", "data": {"bounded": True}},
                [question],
                budget=SemanticBudget(max_requests=1, max_attempts=2, max_retries=1),
            )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.ledger.to_dict()["attempt_count"], 2)
        self.assertEqual(result.usage.to_dict()["total_tokens"], 4)
        self.assertEqual(slept, [0.0])
        self.assertTrue(classify_retry(429))
        self.assertFalse(classify_retry(401))
        self.assertFalse(classify_retry(None, "timeout"))


if __name__ == "__main__":
    unittest.main()
