"""Synthetic routing and checked-diagnosis coverage for stage 2."""

from __future__ import annotations

from types import SimpleNamespace
import json
import unittest
from unittest.mock import patch

from lib.agent_analysis import AgentConfig, call_agent, discover_agent_catalog, operator_catalog
from lib.jev_routing import AnalysisRequest, choose_model, routing_vs_baseline
from lib.postcheck import check_generated_claims
from lib.semantic_backend import GenericSemanticBackend, MockSemanticBackend


def _command(_prompt):
    return ["fixture-analyzer", "--stdin"]


class JevRoutingRegressionTest(unittest.TestCase):
    def _candidate(self, name, priority, *, status="available"):
        return AgentConfig(
            name,
            _command,
            executor="fixture",
            provider="fixture-provider",
            route="fixture.stdin",
            model_id=name.split("/", 1)[-1],
            priority=priority,
            availability={
                "status": status,
                "provenance": "operator",
                "checked_at": "2026-09-20T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )

    def test_discovery_does_not_promote_cli_presence_to_account_access(self):
        candidate = self._candidate("fixture/model", 1, status="available")
        with patch("lib.agent_analysis.shutil.which", return_value="/usr/bin/fixture"):
            discovered = discover_agent_catalog([candidate])
        self.assertEqual(discovered[0].availability["status"], "unknown")
        self.assertEqual(discovered[0].availability["provenance"], "read_only_discovery")
        self.assertTrue(discovered[0].availability["checked_at"])

        promoted = operator_catalog({discovered[0].name: {"status": "available"}}, candidates=[discovered[0]])
        self.assertEqual(promoted[0].availability["status"], "available")
        self.assertEqual(promoted[0].availability["provenance"], "operator")

    def test_jev_choice_selects_a_concrete_candidate(self):
        candidates = [self._candidate("fixture/slow", 20), self._candidate("fixture/fast", 10)]
        request = AnalysisRequest(context_bytes=100, output_bytes=100)

        def handler(state, questions):
            return {"answers": {questions[0]["question_id"]: candidates[0].candidate_id}}

        decision = choose_model(
            candidates,
            request,
            backend=GenericSemanticBackend(handler, model="fixture-jev"),
        )
        self.assertEqual(decision.status, "selected")
        self.assertEqual(decision.routing_source, "jev")
        self.assertEqual(decision.candidate_id, candidates[0].candidate_id)
        self.assertEqual(decision.requested_model, None)

    def test_explicit_override_is_not_silently_replaced(self):
        candidates = [self._candidate("fixture/one", 1), self._candidate("fixture/two", 2)]
        request = AnalysisRequest(context_bytes=10, output_bytes=10, model_override="missing/model")
        decision = choose_model(candidates, request)
        self.assertIsNone(decision.candidate)
        self.assertEqual(decision.routing_source, "override")
        self.assertIn("override_not_found", {item["kind"] for item in decision.diagnostics})

    def test_stdin_adapter_keeps_actual_identity_and_unknown_usage_honest(self):
        candidate = self._candidate("fixture/model", 1)
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"text": "bounded result", "model": "fixture-actual"}),
                stderr="",
            )

        with patch("lib.agent_analysis.subprocess.run", side_effect=fake_run):
            result = call_agent("secret-free prompt", agent_chain=[candidate], max_retries=0)
        self.assertTrue(result.success)
        self.assertNotIn("secret-free prompt", seen["argv"])
        self.assertEqual(seen["input"], "secret-free prompt")
        self.assertEqual(result.requested_model, "model")
        self.assertEqual(result.actual_model, "fixture-actual")
        self.assertIsNone(result.native_usage["total_tokens"])

    def test_failed_execution_reselects_at_most_one_candidate(self):
        first = self._candidate("fixture/first", 1)
        second = self._candidate("fixture/second", 2)
        calls = []

        def fake_run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="failed")
            return SimpleNamespace(returncode=0, stdout="recovered", stderr="")

        with patch("lib.agent_analysis.subprocess.run", side_effect=fake_run):
            result = call_agent("prompt", agent_chain=[first, second], max_retries=1)
        self.assertTrue(result.success)
        self.assertEqual(len(calls), 2)
        self.assertEqual(result.routing.candidate_id, second.candidate_id)
        self.assertEqual(len(result.attempts), 2)

    def test_postcheck_preserves_contradiction_and_bounds_repair_to_one_round(self):
        calls = []

        def handler(state, questions):
            calls.append(questions)
            value = "contradicted" if len(calls) == 1 else "supported"
            return {
                "answers": {question["question_id"]: value for question in questions},
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        evidence = {"cases": [{"case_id": "case-1", "text": "original evidence"}]}
        result = check_generated_claims(
            evidence,
            claims=[{"claim_id": "claim-1", "text": "generated claim"}],
            backend=GenericSemanticBackend(handler),
            repair=lambda item, frozen: {"text": "bounded repaired claim"},
        )
        self.assertEqual(result.checks[0].status, "contradicted")
        self.assertEqual(result.repair_count, 1)
        self.assertEqual(result.checks[0].repair_status, "supported")
        self.assertEqual(result.evidence_hash, result.provenance["evidence_hash"])
        self.assertEqual(len(calls), 2)

    def test_routing_baseline_pilot_keeps_quality_authority_separate(self):
        candidates = [self._candidate("fixture/first", 1), self._candidate("fixture/second", 2)]
        result = routing_vs_baseline(
            [AnalysisRequest(context_bytes=10, output_bytes=10)],
            candidates,
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.denominator, 1)
        self.assertIsNone(result.quality_authority)
        self.assertEqual(result.label_status, "synthetic_expectations_only")


if __name__ == "__main__":
    unittest.main()
