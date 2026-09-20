"""Focused regressions for session-health routing context and catalog evidence."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import lib.agent_analysis as analysis_module
from lib.agent_analysis import (
    AgentAnalysis,
    AgentConfig,
    build_session_health_task_profile,
    call_agent,
    discover_agent_catalog,
    operator_catalog,
)
from lib.jev_routing import AnalysisRequest, RouteDecision


def _command(_prompt: str):
    return ["fixture-analyzer"]


def _candidate(name: str = "fixture/model") -> AgentConfig:
    return AgentConfig(
        name,
        _command,
        executor="fixture",
        provider="fixture",
        route="fixture.prompt",
        model_id=name.split("/", 1)[-1],
        availability={
            "status": "available",
            "provenance": "operator",
            "checked_at": "2026-09-20T00:00:00Z",
            "expires_at": "2099-01-01T00:00:00Z",
        },
    )


class RoutingContextTest(unittest.TestCase):
    def _capture_route(self, *, request=None, task_profile=None, model_override=""):
        captured = []
        candidate = _candidate()

        def choose(candidates, actual_request, **_kwargs):
            captured.append(actual_request)
            selected = candidates[0]
            return RouteDecision(
                status="selected",
                routing_source="test",
                candidate=selected,
                candidate_id=selected.candidate_id,
            )

        with patch.object(analysis_module, "choose_model", side_effect=choose), patch.object(
            analysis_module,
            "_execute_candidate",
            return_value=AgentAnalysis(agent_name=candidate.name, success=True),
        ):
            result = call_agent(
                "opaque prompt marker",
                agent_chain=[candidate],
                request=request,
                task_profile=task_profile,
                model_override=model_override,
                use_jev=False,
                max_retries=0,
            )
        self.assertTrue(result.success)
        self.assertEqual(len(captured), 1)
        return captured[0]

    def test_default_caller_profile_reaches_routing_without_prompt(self):
        request = self._capture_route()
        profile = request.task_profile
        self.assertEqual(profile["scope"], "single")
        self.assertEqual(profile["count"], 1)
        self.assertEqual(set(profile["axes"]), {"SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL"})
        self.assertEqual(profile["analysis_output"]["language"], "zh-TW")
        self.assertEqual(profile["model_suitability"]["decision"], "provisional_suitability")
        self.assertNotIn("opaque prompt marker", json.dumps(profile, ensure_ascii=False))

    def test_custom_profile_is_preserved_and_explicit_profile_overrides(self):
        custom = AnalysisRequest(
            context_bytes=1,
            output_bytes=1,
            task_profile={"scope": "custom", "marker": "preserve"},
        )
        preserved = self._capture_route(request=custom, model_override="fixture/override")
        self.assertEqual(preserved.task_profile, {"scope": "custom", "marker": "preserve"})
        self.assertEqual(preserved.model_override, "fixture/override")

        overridden = self._capture_route(
            request=custom,
            task_profile={"scope": "batch", "count": 3},
        )
        self.assertEqual(overridden.task_profile, {"scope": "batch", "count": 3})

    def test_capability_evidence_is_sanitized_bounded_and_kept_in_operator_card(self):
        evidence = {
            "provenance": "provider_advertised",
            "description": "/home/paul_chen/private description",
            "api_key": "do-not-export",
            "nested": {"text": "x" * 20_000},
        }
        candidate = AgentConfig("fixture/model", _command, capability_evidence=evidence)
        payload = candidate.to_dict()
        encoded = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["capability_evidence"]["provenance"], "provider_advertised")
        self.assertNotIn("do-not-export", encoded)
        self.assertNotIn("/home/paul_chen", encoded)
        self.assertLess(len(encoded.encode("utf-8")), 9_000)
        self.assertEqual(candidate.clone().to_dict(), payload)

        configured = operator_catalog(
            [
                {
                    "name": "copilot/evidence-model",
                    "executor": "copilot",
                    "provider": "github",
                    "route": "copilot.prompt",
                    "model_id": "evidence-model",
                    "inference_settings": {"stdin": True},
                    "status": "unknown",
                    "capability_evidence": evidence,
                }
            ],
            candidates=[],
        )
        self.assertEqual(configured[0].to_dict()["capability_evidence"]["provenance"], "provider_advertised")

    def test_default_cache_adds_visible_unknown_candidates_and_degrades_safely(self):
        with tempfile.TemporaryDirectory() as directory:
            cache_path = Path(directory) / "models_cache.json"
            cache_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-cache-visible",
                                "display_name": "Cached Visible",
                                "description": "advertised only",
                                "visibility": "list",
                                "context_window": 272000,
                                "default_reasoning_level": "high",
                            },
                            {"slug": "gpt-cache-hidden", "visibility": "hide"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            discovered = discover_agent_catalog(model_cache_path=cache_path)
            visible = next(item for item in discovered if item.model_id == "gpt-cache-visible")
            self.assertEqual(visible.availability["status"], "unknown")
            self.assertEqual(visible.availability["provenance"], "read_only_discovery")
            self.assertIsNone(visible.context_window)
            self.assertEqual(visible.capabilities, ())
            self.assertEqual(
                visible.to_dict()["capability_evidence"]["advertised"]["context_window"],
                {"value": 272000, "unit": "provider_tokens", "status": "advertised"},
            )
            self.assertFalse(any(item.model_id == "gpt-cache-hidden" for item in discovered))

            explicit = discover_agent_catalog([_candidate()], model_cache_path=cache_path)
            self.assertEqual([item.model_id for item in explicit], ["model"])

            bad_path = Path(directory) / "bad.json"
            bad_path.write_text("{bad", encoding="utf-8")
            defaults = {item.name for item in discover_agent_catalog(model_cache_path=bad_path)}
            self.assertTrue(defaults)
            self.assertNotIn("codex/gpt-cache-visible", defaults)


if __name__ == "__main__":
    unittest.main()
