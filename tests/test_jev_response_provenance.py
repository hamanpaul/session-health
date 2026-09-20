"""Regression coverage for Jev served-model response provenance."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import patch

from lib.semantic_backend import JevHTTPBackend, SemanticBudget, SemanticQuestion, stable_hash


class _Response:
    status = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def read(self, limit=-1):
        if isinstance(self.payload, bytes):
            return self.payload
        return json.dumps(self.payload).encode("utf-8")


class JevResponseProvenanceTest(unittest.TestCase):
    def _choice_question(self, question_id="choice"):
        return SemanticQuestion(
            question_id=question_id,
            axis_id="SNR",
            prompt="Classify the evidence.",
            answer_type="choice",
            choices=("relevant", "irrelevant"),
            case_id="case-1",
        )

    def _noul_question(self, question_id="noul"):
        return SemanticQuestion(
            question_id=question_id,
            axis_id="STATE",
            prompt="Is the state sufficient?",
            answer_type="noul",
            case_id="case-1",
        )

    def _evaluate(self, payload, questions):
        with patch.dict(os.environ, {"TYPESAFE_API_KEY": "fixture-key"}, clear=False):
            return JevHTTPBackend(
                model="jev-latest",
                opener=lambda request, timeout: _Response(payload),
            ).evaluate(
                {"state_id": "state-1", "data": {"bounded": True}},
                questions,
                budget=SemanticBudget(max_requests=1, max_attempts=1, max_retries=0),
            )

    def test_success_preserves_served_model_hashes_and_native_metadata(self):
        payload = {
            "model": "jev-1.13.0",
            "answers": {
                "choice": {
                    "type": "choice",
                    "choice": "relevant",
                    "probabilities": {"relevant": 0.8, "irrelevant": 0.2},
                    "confidence": 0.61,
                }
            },
            "usage": {
                "input_tokens": 409,
                "output_tokens": 22,
                "total_tokens": 431,
                "cached_tokens": 7,
                "reasoning_tokens": 3,
            },
        }

        result = self._evaluate(payload, [self._choice_question()])

        self.assertEqual(result.status, "complete")
        self.assertEqual(result.metadata["requested_model"], "jev-latest")
        self.assertEqual(result.metadata["actual_model"], "jev-1.13.0")
        self.assertEqual(result.metadata["request_id"], result.provenance["request_id"])
        self.assertEqual(result.metadata["response_hash"], stable_hash(payload))
        self.assertEqual(result.ledger.attempts[0].response_hash, stable_hash(payload))
        self.assertEqual(result.answers["choice"].probabilities, {"relevant": 0.8, "irrelevant": 0.2})
        self.assertEqual(result.answers["choice"].confidence, 0.61)
        self.assertEqual(result.usage.to_dict(), payload["usage"])

        serialized = result.to_dict()
        self.assertEqual(serialized["metadata"], result.metadata)
        self.assertEqual(serialized["metadata"]["actual_model"], "jev-1.13.0")
        self.assertEqual(serialized["usage"], payload["usage"])
        self.assertEqual(serialized["answers"]["choice"]["probabilities"], payload["answers"]["choice"]["probabilities"])

    def test_validated_partial_without_model_keeps_actual_identity_unknown(self):
        payload = {
            "answers": {
                "noul": {"type": "noul", "noul": 0.75},
            },
            "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        }

        result = self._evaluate(payload, [self._noul_question(), self._choice_question()])

        self.assertEqual(result.status, "partial")
        self.assertEqual(result.answers["noul"].value, 0.75)
        self.assertEqual(result.metadata["requested_model"], "jev-latest")
        self.assertIsNone(result.metadata["actual_model"])
        self.assertEqual(result.metadata["request_id"], result.provenance["request_id"])
        self.assertEqual(result.metadata["response_hash"], stable_hash(payload))
        self.assertEqual(result.to_dict()["metadata"]["actual_model"], None)


if __name__ == "__main__":
    unittest.main()
