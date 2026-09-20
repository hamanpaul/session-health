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
            backend.evaluate({"state_id": "s", "data": {"bounded": True}}, [question], budget=SemanticBudget(max_requests=2, max_attempts=2, max_retries=0))
            second = backend.evaluate({"state_id": "s", "data": {"bounded": True}}, [question], budget=SemanticBudget(max_requests=2, max_attempts=2, max_retries=0))

        self.assertEqual(len(calls), 2)
        self.assertEqual(second.ledger.request_count, 2)
        self.assertEqual(second.ledger.attempt_count, 2)
        self.assertEqual(second.usage.to_dict()["total_tokens"], 12)
        self.assertNotEqual(second.ledger.attempts[0].attempt_id, second.ledger.attempts[1].attempt_id)

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
