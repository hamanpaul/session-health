"""Focused regressions for the bounded original stage-2 integration repair."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import eval_session
from lib.agent_analysis import (
    AgentAnalysis,
    AgentConfig,
    _build_agy_cmd,
    build_repair_callback,
    operator_catalog,
    prepare_analysis_prompt,
    prepare_batch_analysis_prompt,
    call_agent,
)
from lib.jev_routing import RouteDecision, AnalysisRequest, choose_model
from lib.parser_base import Session, Turn
from lib.postcheck import evidence_hash, postcheck_analysis
from lib.scorer import score_session
from lib.semantic_backend import GenericSemanticBackend, SemanticBudget


ROOT = Path(__file__).resolve().parents[1]


def _session() -> Session:
    return Session(
        id="stage2-fixture",
        source="codex",
        model="fixture-model",
        source_ref="fixture.jsonl#L1",
        turns=[Turn(index=0, user_input="請檢查 bounded evidence。")],
    )


def _available_agy() -> AgentConfig:
    return AgentConfig(
        "agy/gemini-3.8-flash-high",
        _build_agy_cmd,
        executor="agy",
        provider="google",
        route="agy.prompt",
        model_id="gemini-3.8-flash-high",
        inference_settings={"effort": "high", "prompt_transport": "argv"},
        availability={
            "status": "available",
            "provenance": "operator",
            "checked_at": "2026-09-20T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )


class Stage2WiringTest(unittest.TestCase):
    def test_single_prompt_contains_independent_stage2_layers_and_redacts(self):
        score = score_session(_session())
        private_ref = "/".join(("", "home", "synthetic-user", "private.jsonl"))
        bundle = {
            "manifest": {"schema": "session-health.session-bundle", "source_ref": "fixture.jsonl#L1"},
            "facts": {"metric_facts": {"system": "system-secret", "api_key": "do-not-send"}},
            "events": [
                {
                    "kind": "system_message",
                    "payload": {"text": "system-secret"},
                    "source_ref": private_ref + "#L2",
                },
                {
                    "kind": "tool_result",
                    "payload": {"output": "observed output"},
                    "source_ref": "fixture.jsonl#L3",
                },
            ],
            "evidence_refs": [{"ref_id": "evidence-1", "text": "bounded"}],
            "coverage": {"observed_cutoff": "2026-09-20T00:00:00Z", "status": "complete"},
        }
        process_v2 = {
            "status": "complete",
            "axes": {
                "STATE": {
                    "metric": {"value": 0.5, "status": "observed"},
                    "observed_facts": {"emitted_fields": ["cwd_present"]},
                    "inference": {"quality_judgment": None},
                    "evidence_refs": ["evidence-1"],
                }
            },
        }
        semantic = {
            "status": "partial",
            "coverage": {"semantic_coverage": 0.5, "coverage_status": "partial"},
            "questions": [
                {
                    "question_id": "q-missing",
                    "axis_id": "STATE",
                    "case_id": "case-1",
                    "stage": 1,
                    "depends_on": [],
                    "evidence_refs": ["evidence-1"],
                    "metadata": {"observation_cutoff": "2026-09-20T00:00:00Z"},
                }
            ],
            "answers": {
                "q-adopted": {
                    "question_id": "q-adopted",
                    "primitive": "choice",
                    "value": "supported",
                    "status": "observed",
                    "applicability": "applicable",
                    "evidence_refs": ["evidence-1"],
                    "counterevidence_refs": ["counter-1"],
                }
            },
            "provenance": {"semantic_version": "semantic-v1", "state_hash": "state-hash"},
        }

        prompt = prepare_analysis_prompt(
            score,
            _session(),
            process_v2=process_v2,
            bundle=bundle,
            semantic=semantic,
        )

        self.assertIn("Independent stage-2 evidence layers", prompt)
        self.assertIn("adopted_judgments", prompt)
        self.assertIn("counterevidence_refs", prompt)
        self.assertIn("missing_or_unadopted", prompt)
        self.assertIn("observation_cutoff", prompt)
        self.assertIn("emitted_fields", prompt)
        self.assertNotIn("system-secret", prompt)
        self.assertNotIn("do-not-send", prompt)
        self.assertNotIn(private_ref, prompt)
        self.assertIn('"observations"', prompt)
        self.assertIn('"recommendations"', prompt)

    def test_native_envelope_markdown_reaches_postcheck(self):
        candidate = _available_agy()
        response = {
            "response": (
                "### Observations\n"
                "* **Observed state**: the bounded fact is present.\n\n"
                "### Hypotheses\n"
                "* **H1**: a missing trace may explain the gap.\n\n"
                "### Recommendations\n"
                "1. Add a bounded verification step.\n"
            ),
            "usage": {"input_tokens": 7, "output_tokens": 5, "total_tokens": 12},
        }

        with patch(
            "lib.agent_analysis.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response), stderr=""),
        ):
            analysis = call_agent("bounded prompt", agent_chain=[candidate], use_jev=False, max_retries=0)

        self.assertTrue(analysis.success)
        self.assertEqual(len(analysis.claims), 2)
        self.assertEqual(len(analysis.recommendations), 1)
        self.assertEqual(analysis.native_usage["total_tokens"], 12)

        def handler(_state, questions):
            return {
                "answers": {question["question_id"]: "supported" for question in questions},
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            }

        result = postcheck_analysis(
            {"cases": [{"case_id": "case-1", "text": "original evidence"}]},
            analysis,
            backend=GenericSemanticBackend(handler),
            budget=SemanticBudget(max_requests=1, max_attempts=1, max_questions=8, max_cases=8),
        )
        self.assertEqual(result.status, "complete")
        self.assertEqual(result.claim_count, 3)
        self.assertEqual(result.checked_count, 3)

    def test_batch_prompt_keeps_per_session_stage2_context(self):
        prompt = prepare_batch_analysis_prompt(
            {"session_count": 1},
            [{"session_id": "batch-1", "score": 50, "grade": "F"}],
            stage2_contexts=[
                {
                    "bundle": {"evidence_refs": ["evidence-1"]},
                    "process_v2": {"status": "complete"},
                    "semantic": {
                        "adopted_judgments": [{"question_id": "q-1", "counterevidence_refs": ["counter-1"]}],
                        "missing_or_unadopted": [{"question_id": "q-2", "observation_cutoff": "cutoff-1"}],
                    },
                }
            ],
        )
        self.assertIn("evidence-1", prompt)
        self.assertIn("q-1", prompt)
        self.assertIn("counter-1", prompt)
        self.assertIn("q-2", prompt)
        self.assertIn("cutoff-1", prompt)

    def test_nonempty_unparseable_output_is_partial(self):
        analysis = AgentAnalysis(
            success=True,
            raw_response="This is prose without a finding section.",
            structured_output={"response": "This is prose without a finding section."},
        )
        result = postcheck_analysis({"cases": []}, analysis)
        self.assertEqual(result.status, "partial")
        self.assertEqual(result.claim_count, 0)
        self.assertIn("unparseable_generated_output", {item["kind"] for item in result.diagnostics})

    def test_generated_evidence_and_counterevidence_refs_survive_postcheck(self):
        analysis = AgentAnalysis(
            success=True,
            structured_output={
                "claims": [
                    {
                        "text": "bounded conclusion",
                        "evidence_refs": ["evidence-1"],
                        "counterevidence_refs": ["counter-1"],
                    }
                ],
                "recommendations": [],
            },
        )

        def handler(_state, questions):
            return {"answers": {question["question_id"]: "supported" for question in questions}}

        result = postcheck_analysis(
            {"cases": [{"case_id": "case-1", "text": "original evidence"}]},
            analysis,
            backend=GenericSemanticBackend(handler),
            budget=SemanticBudget(max_requests=1, max_attempts=1, max_questions=2, max_cases=2),
        )
        self.assertEqual(result.checks[0].evidence_refs, ["evidence-1"])
        self.assertEqual(result.checks[0].counterevidence_refs, ["counter-1"])

    def test_repair_reuses_frozen_evidence_and_aggregate_budget(self):
        candidate = _available_agy()
        analysis = AgentAnalysis(
            agent_name=candidate.name,
            success=True,
            claims=[{"claim_id": "claim-1", "text": "overstated finding"}],
            routing=RouteDecision(status="selected", candidate=candidate, candidate_id=candidate.candidate_id),
        )
        repair_response = {"response": "bounded repaired finding"}
        with patch(
            "lib.agent_analysis.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout=json.dumps(repair_response), stderr=""),
        ):
            repair = build_repair_callback(analysis)

            calls = []

            def handler(_state, questions):
                calls.append(questions)
                value = "contradicted" if len(calls) == 1 else "supported"
                return {
                    "answers": {question["question_id"]: value for question in questions},
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                }

            evidence = {"cases": [{"case_id": "case-1", "text": "original evidence"}]}
            frozen = {"cases": [{"case_id": "case-1", "text": "original evidence"}]}
            result = postcheck_analysis(
                frozen,
                analysis,
                backend=GenericSemanticBackend(handler),
                budget=SemanticBudget(max_requests=2, max_attempts=2, max_questions=4, max_cases=4),
                repair=repair,
            )

        self.assertEqual(result.evidence_hash, evidence_hash(frozen))
        self.assertEqual(result.repair_count, 1)
        self.assertEqual(result.checks[0].repair_status, "supported")
        self.assertEqual(len(analysis.repair_attempts), 1)
        self.assertEqual(result.ledger["request_count"], 2)

    def test_operator_catalog_constructs_luna_max_and_keeps_unknown_hard_gate(self):
        cards = [
            {
                "name": "codex/gpt-5.6-luna",
                "executor": "codex",
                "provider": "openai",
                "route": "codex.exec",
                "model_id": "gpt-5.6-luna",
                "inference_settings": {"effort": "max", "stdin": True},
                "status": "available",
                "priority": 1,
            }
        ]
        catalog = operator_catalog(cards, candidates=[])
        self.assertEqual(len(catalog), 1)
        luna = catalog[0]
        self.assertEqual(luna.name, "codex/gpt-5.6-luna")
        self.assertEqual(luna.inference_settings["effort"], "max")
        self.assertEqual(
            luna.build_cmd("ignored because Codex uses stdin"),
            ["codex", "-c", "model=gpt-5.6-luna", "-c", "model_reasoning_effort=max", "exec", "-"],
        )
        decision = choose_model(
            catalog,
            AnalysisRequest(context_bytes=10, output_bytes=10),
            use_jev=False,
        )
        self.assertEqual(decision.candidate_id, luna.candidate_id)

        unknown_catalog = operator_catalog(
            [{key: value for key, value in cards[0].items() if key != "status"}],
            candidates=[],
        )
        unknown = choose_model(
            unknown_catalog,
            AnalysisRequest(context_bytes=10, output_bytes=10),
            use_jev=False,
        )
        self.assertIsNone(unknown.candidate)
        self.assertIn("availability_unknown", unknown.eligibility[0].reasons)
        override = choose_model(
            unknown_catalog,
            AnalysisRequest(context_bytes=10, output_bytes=10, model_override="codex/gpt-5.6-luna"),
            use_jev=False,
        )
        self.assertEqual(override.candidate_id, unknown_catalog[0].candidate_id)

    def test_production_catalog_cli_is_read_only_and_uses_explicit_card(self):
        cards = [
            {
                "name": "codex/gpt-5.6-luna",
                "executor": "codex",
                "provider": "openai",
                "route": "codex.exec",
                "model_id": "gpt-5.6-luna",
                "inference_settings": {"effort": "max", "stdin": True},
                "status": "available",
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            catalog_path = Path(directory) / "catalog.json"
            catalog_path.write_text(json.dumps(cards), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(ROOT / "eval_session.py"), "--list-models", "--model-catalog-file", str(catalog_path)],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        luna = next(item for item in payload if item["name"] == "codex/gpt-5.6-luna")
        self.assertEqual(luna["inference_settings"]["effort"], "max")
        self.assertEqual(luna["availability"]["provenance"], "operator")

    def test_production_cli_routes_operator_luna_card_to_fake_codex(self):
        records = [
            {"type": "session_meta", "payload": {"id": "luna-stage2", "model": "fixture"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "fixture"}]}},
        ]
        cards = [
            {
                "name": "codex/gpt-5.6-luna",
                "executor": "codex",
                "provider": "openai",
                "route": "codex.exec",
                "model_id": "gpt-5.6-luna",
                "inference_settings": {"effort": "max", "stdin": True},
                "status": "available",
                "priority": 1,
            }
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "session.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            catalog_path = root / "catalog.json"
            catalog_path.write_text(json.dumps(cards), encoding="utf-8")
            fake_codex = root / "codex"
            fake_codex.write_text(
                "#!/usr/bin/env python3\n"
                "import json, sys\n"
                "sys.stdin.read()\n"
                "print(json.dumps({'response': '### Observations\\n* bounded finding', 'model': 'codex-actual', 'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5}}))\n",
                encoding="utf-8",
            )
            fake_codex.chmod(0o755)
            stdout = io.StringIO()
            with patch.dict(os.environ, {"PATH": directory + os.pathsep + os.environ.get("PATH", "")}), \
                    patch("sys.argv", ["eval_session", str(source), "--analyze", "--model-catalog-file", str(catalog_path), "--format", "json"]), \
                    contextlib.redirect_stdout(stdout):
                eval_session.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["agent_analysis"]["requested_model"], "gpt-5.6-luna")
        self.assertEqual(payload["agent_analysis"]["requested_settings"]["effort"], "max")
        self.assertEqual(payload["agent_analysis"]["actual_model"], "codex-actual")
        self.assertEqual(payload["agent_analysis"]["native_usage"]["total_tokens"], 5)
        self.assertEqual(payload["routing"]["candidate"]["name"], "codex/gpt-5.6-luna")

    def test_offline_analyze_batch_never_invokes_agent(self):
        records = [
            {"type": "session_meta", "payload": {"id": "offline-stage2", "model": "fixture"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "fixture"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            second = Path(directory) / "session-second.jsonl"
            second.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "offline-stage2-second", "model": "fixture"}}),
                        json.dumps({"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "fixture two"}]}}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with patch("sys.argv", ["eval_session", "--dir", directory, "--analyze", "--offline", "--format", "json"]), \
                    patch.object(eval_session, "call_agent", side_effect=AssertionError("offline analyzer call")) as analyzer, \
                    contextlib.redirect_stdout(stdout):
                eval_session.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["analysis_status"], "disabled_offline")
        self.assertEqual(len(payload["sessions"]), 2)
        self.assertTrue(all(item["analysis_status"] == "disabled_offline" for item in payload["sessions"]))
        analyzer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
