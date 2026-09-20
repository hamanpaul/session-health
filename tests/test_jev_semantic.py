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
        self.assertTrue(all(question.state_path.startswith("state.cases.") for question in questions))
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
