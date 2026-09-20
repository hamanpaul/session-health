"""Synthetic routing and checked-diagnosis coverage for stage 2."""

from __future__ import annotations

from types import SimpleNamespace
import os
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lib.agent_analysis as agent_analysis_module
from lib.agent_analysis import AgentConfig, call_agent, discover_agent_catalog, operator_catalog
from lib.agent_analysis import _build_agy_cmd
from lib.jev_routing import AnalysisRequest, choose_model, routing_vs_baseline
from lib.postcheck import check_generated_claims
from lib.semantic_backend import GenericSemanticBackend, MockSemanticBackend, SemanticBudget


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

    def test_agy_adapter_uses_supported_print_stdin_contract_and_native_usage(self):
        candidate = AgentConfig(
            "agy/gemini-3.8-flash-high",
            _build_agy_cmd,
            executor="agy",
            provider="google",
            route="agy.prompt",
            model_id="gemini-3.8-flash-high",
            inference_settings={"effort": "high", "stdin": True},
            availability={
                "status": "available",
                "provenance": "operator",
                "checked_at": "2026-09-20T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        seen = {}

        def fake_run(argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    {
                        "response": "bounded result",
                        "usage": {"input_tokens": 4, "output_tokens": 3, "total_tokens": 7},
                    }
                ),
                stderr="",
            )

        with patch("lib.agent_analysis.subprocess.run", side_effect=fake_run):
            result = call_agent("secret-free prompt", agent_chain=[candidate], max_retries=0)
        self.assertTrue(result.success)
        self.assertNotIn("--input", seen["argv"])
        self.assertEqual(
            seen["argv"],
            [
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
            ],
        )
        self.assertNotIn("secret-free prompt", seen["argv"])
        self.assertEqual(seen["input"], "secret-free prompt")
        self.assertEqual(result.raw_response, "bounded result")
        self.assertEqual(result.native_usage["total_tokens"], 7)

    def test_agy_adapter_delivers_prompt_to_real_subprocess_stdin(self):
        candidate = AgentConfig(
            "agy/gemini-3.8-flash-high",
            _build_agy_cmd,
            executor="agy",
            provider="google",
            route="agy.prompt",
            model_id="gemini-3.8-flash-high",
            inference_settings={"effort": "high", "stdin": True},
            availability={
                "status": "available",
                "provenance": "operator",
                "checked_at": "2026-09-20T00:00:00Z",
                "expires_at": "2099-01-01T00:00:00Z",
            },
        )
        prompt = "bounded prompt marker\nsecond line"
        expected_argv = [
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

        with tempfile.TemporaryDirectory() as tempdir:
            fake_agy = Path(tempdir) / "agy"
            fake_agy.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import sys\n"
                "prompt = sys.stdin.read()\n"
                "print(json.dumps({\n"
                "    'response': prompt,\n"
                "    'model': 'fake-actual',\n"
                "    'usage': {'input_tokens': 4, 'output_tokens': 3, 'total_tokens': 7},\n"
                "    'argv': sys.argv[1:],\n"
                "}))\n",
                encoding="utf-8",
            )
            fake_agy.chmod(0o755)
            path = os.pathsep.join([tempdir, os.environ.get("PATH", "")])
            with patch.dict(os.environ, {"PATH": path}):
                result = call_agent(prompt, agent_chain=[candidate], max_retries=0)

        self.assertTrue(result.success)
        self.assertEqual(result.raw_response, prompt)
        self.assertEqual(result.actual_model, "fake-actual")
        self.assertEqual(result.native_usage["total_tokens"], 7)
        self.assertEqual(result.structured_output["response"], prompt)
        self.assertEqual(result.structured_output["argv"], expected_argv)

    def test_failed_execution_does_not_mutate_catalog_across_invocations(self):
        candidate = self._candidate("fixture/model", 1)
        failed = SimpleNamespace(returncode=1, stdout="", stderr="failed")
        with patch.object(agent_analysis_module, "AGENT_CHAIN", [candidate]), patch(
            "lib.agent_analysis.subprocess.run", return_value=failed
        ):
            first = call_agent("prompt", max_retries=0, use_jev=False)
            second = call_agent("prompt", max_retries=0, use_jev=False)
        self.assertFalse(first.success)
        self.assertFalse(second.success)
        self.assertEqual(first.routing.candidate_id, second.routing.candidate_id)
        self.assertEqual(candidate.availability["status"], "available")

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

        def handler(state, questions):
            context_bytes = state["data"]["routing"]["request"]["context_bytes"]
            chosen = candidates[1] if context_bytes == 10 else candidates[0]
            return {"answers": {questions[0]["question_id"]: chosen.candidate_id}}

        requests = [
            AnalysisRequest(context_bytes=10, output_bytes=10),
            AnalysisRequest(context_bytes=20, output_bytes=10),
        ]
        result = routing_vs_baseline(
            requests,
            candidates,
            backend=GenericSemanticBackend(handler, model="fixture-pilot"),
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.denominator, 2)
        self.assertEqual(result.valid_case_count, 2)
        self.assertEqual(result.agreement, 0.5)
        self.assertIsNone(result.quality_authority)
        self.assertEqual(result.label_status, "synthetic_expectations_only")
        self.assertTrue(all(item["comparison_status"] == "valid" for item in result.cases))

        reverse = routing_vs_baseline(
            list(reversed(requests)),
            candidates,
            backend=GenericSemanticBackend(handler, model="fixture-pilot"),
        )
        forward_choices = {
            item["request_id"]: item["routed"]["candidate_id"] for item in result.cases
        }
        reverse_choices = {
            item["request_id"]: item["routed"]["candidate_id"] for item in reverse.cases
        }
        self.assertEqual(reverse.status, "complete")
        self.assertEqual(reverse.agreement, 0.5)
        self.assertEqual(forward_choices, reverse_choices)

    def test_routing_budget_failure_is_partial_and_not_agreement(self):
        candidates = [self._candidate("fixture/first", 1), self._candidate("fixture/second", 2)]

        def handler(_state, questions):
            return {"answers": {questions[0]["question_id"]: candidates[1].candidate_id}}

        result = routing_vs_baseline(
            [
                AnalysisRequest(context_bytes=10, output_bytes=10),
                AnalysisRequest(context_bytes=20, output_bytes=10),
            ],
            candidates,
            backend=GenericSemanticBackend(handler, model="fixture-budget"),
            budget=SemanticBudget(max_requests=1, max_attempts=1, max_questions=1, max_cases=1, max_retries=0),
        )
        self.assertEqual(result.status, "partial")
        self.assertIsNone(result.agreement)
        self.assertEqual(result.valid_case_count, 1)
        self.assertIsNone(result.cases[1]["agreement"])
        self.assertIn(
            "jev_routing_exception",
            {item["kind"] for item in result.cases[1]["routed"]["diagnostics"]},
        )

    def test_routing_reuses_remaining_budget_after_prior_semantic_stage(self):
        candidates = [self._candidate("fixture/first", 1), self._candidate("fixture/second", 2)]

        def handler(state, questions):
            context_bytes = state["data"]["routing"]["request"]["context_bytes"]
            chosen = candidates[1] if context_bytes == 10 else candidates[0]
            return {"answers": {questions[0]["question_id"]: chosen.candidate_id}}

        backend = GenericSemanticBackend(handler, model="fixture-stage-separation")
        stage_budget = SemanticBudget(max_requests=3, max_attempts=3, max_questions=1, max_cases=1, max_retries=0)
        stage_one = choose_model(
            candidates,
            AnalysisRequest(context_bytes=5, output_bytes=10),
            backend=backend,
            budget=stage_budget,
        )
        self.assertEqual(stage_one.routing_source, "jev")
        result = routing_vs_baseline(
            [
                AnalysisRequest(context_bytes=10, output_bytes=10),
                AnalysisRequest(context_bytes=20, output_bytes=10),
            ],
            candidates,
            backend=backend,
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(backend.ledger.request_count, 3)
        self.assertFalse(
            any(
                item["kind"] == "jev_routing_exception"
                for case in result.cases
                for item in case["routed"]["diagnostics"]
            )
        )

    def test_routing_pilot_without_backend_is_not_applicable(self):
        candidates = [self._candidate("fixture/first", 1), self._candidate("fixture/second", 2)]
        result = routing_vs_baseline([AnalysisRequest(context_bytes=10, output_bytes=10)], candidates)
        self.assertEqual(result.status, "not_applicable")
        self.assertIsNone(result.agreement)
        self.assertEqual(result.cases[0]["comparison_status"], "invalid")


if __name__ == "__main__":
    unittest.main()
