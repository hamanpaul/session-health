"""Headless hard-filter and bounded consensus tests."""

from __future__ import annotations

import unittest

from lib.agent_analysis import AgentConfig
from lib.headless_routing import select_headless_model
from lib.jev_routing import AnalysisRequest


def _candidate(name: str, priority: int = 10, status: str = "available") -> AgentConfig:
    return AgentConfig(
        name=name,
        build_cmd=lambda _prompt: ["true"],
        executor="codex",
        provider="fixture",
        route="fixture",
        model_id=name,
        availability={"status": status, "stale": False},
        context_window=200_000,
        max_output_bytes=64_000,
        timeout=30,
        priority=priority,
    )


class HeadlessRoutingTest(unittest.TestCase):
    def test_majority_selects_eligible_candidate_and_caps_judges(self):
        first = _candidate("first", 1)
        second = _candidate("second", 2)
        decision = select_headless_model(
            [first, second],
            AnalysisRequest(context_bytes=100, output_bytes=100),
            judge=lambda _judge, _prompt: {"candidate_id": second.candidate_id, "confidence": 0.8, "reason": "fit"},
            jev_receipt={"candidate_id": first.candidate_id, "confidence": 0.6, "reason": "typed vote"},
        )
        self.assertEqual(decision.candidate_id, second.candidate_id)
        self.assertEqual(len(decision.judge_receipts), 3)

    def test_illegal_ids_and_abstention_do_not_select(self):
        first = _candidate("first")
        decision = select_headless_model(
            [first],
            AnalysisRequest(context_bytes=100, output_bytes=100),
            judge=lambda _judge, _prompt: {"candidate_id": "invented", "confidence": 1.0},
            jev_receipt={"candidate_id": "insufficient_model_evidence", "reason": "missing"},
        )
        self.assertIsNone(decision.selected)
        self.assertTrue(any(item["status"] == "illegal_candidate" for item in decision.judge_receipts))

    def test_unknown_or_stale_candidates_are_hard_rejected(self):
        unknown = _candidate("unknown", status="unknown")
        stale = _candidate("stale")
        stale.availability["stale"] = True
        decision = select_headless_model(
            [unknown, stale],
            AnalysisRequest(context_bytes=100, output_bytes=100),
            judge=lambda _judge, _prompt: {},
        )
        self.assertEqual(decision.status, "no_suitable_model")
        self.assertEqual(decision.eligible_candidate_ids, [])

    def test_judge_exception_receipt_does_not_persist_exception_message(self):
        first = _candidate("first")

        def fail(_judge, _prompt):
            raise RuntimeError("opaque-detail-123")

        decision = select_headless_model(
            [first],
            AnalysisRequest(context_bytes=100, output_bytes=100),
            judge=fail,
        )
        receipt = decision.judge_receipts[0]
        self.assertEqual(receipt["reason"], "RuntimeError")
        self.assertNotIn("opaque-detail-123", str(receipt))


if __name__ == "__main__":
    unittest.main()
