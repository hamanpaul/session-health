"""Acceptance tests for the portable offline slice."""

from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eval_session
from lib.bundle import BundleLimits, SessionBundle, build_session_bundle
from lib.metrics.process_v2 import AXIS_IDS, analyze_process_v2, join_external_outcome
from lib.parser_base import Session, ToolCall, Turn
from lib.parser_codex import parse_codex_session


FIXTURES = Path(__file__).parent / "fixtures"


class OfflineBundleAndMetricsTest(unittest.TestCase):
    def test_bundle_round_trip_is_portable_and_redacted(self):
        session = parse_codex_session(FIXTURES / "codex_nested_call_result.jsonl")
        session.turns[0].assistant_output = "token=do-not-export"

        bundle = build_session_bundle(session)
        encoded = bundle.to_json()
        restored = SessionBundle.from_json(encoded)
        replayed = restored.to_session()

        self.assertEqual(replayed.id, session.id)
        self.assertEqual(replayed.turn_count, session.turn_count)
        self.assertNotIn("do-not-export", encoded)
        self.assertNotIn("/tmp/session-health", encoded)
        self.assertEqual(bundle.manifest["source_ref"], "codex_nested_call_result.jsonl")
        self.assertTrue(all("/" not in str(event["source_ref"]) for event in bundle.events))

    def test_bundle_limit_reports_truncation_or_rejects_oversize(self):
        session = Session(
            id="bounded",
            source="codex",
            turns=[Turn(index=1, user_input="run", tool_calls=[ToolCall(name="bash", call_id="1", output="ok")])],
        )
        bundle = build_session_bundle(session, limits=BundleLimits(max_events=1, max_bytes=100_000))
        self.assertEqual(bundle.coverage["status"], "truncated")
        self.assertGreater(bundle.coverage["excluded_events"], 0)

    def test_process_v2_has_seven_axes_and_nulls_missing_denominators(self):
        session = Session(id="one-turn", source="codex", turns=[Turn(index=1, user_input="hello")])
        result = analyze_process_v2(session)

        self.assertEqual(set(result.axes), set(AXIS_IDS))
        self.assertIsNone(result.axes["SNR"].metric.value)
        self.assertIsNone(result.axes["CONV"].metric.denominator)
        self.assertIsNone(result.axes["DEPTH"].metric.value)
        self.assertIsNone(result.inference["correctness_judgment"])

    def test_process_v2_does_not_use_assistant_text_length_as_depth(self):
        short = Session(id="short", source="codex", turns=[Turn(index=1, tool_calls=[ToolCall(name="bash", call_id="1", output="ok")], assistant_output="x")])
        long = Session(id="long", source="codex", turns=[Turn(index=1, tool_calls=[ToolCall(name="bash", call_id="1", output="ok")], assistant_output="x" * 10_000)])
        self.assertEqual(
            analyze_process_v2(short).axes["DEPTH"].metric.value,
            analyze_process_v2(long).axes["DEPTH"].metric.value,
        )

    def test_external_outcome_requires_exact_identity(self):
        session = Session(id="session-a", source="codex", metadata={"task_id": "task-a"})
        matched = join_external_outcome(session, {"session_id": "session-a", "verdict": "pass", "authority": "fixture"})
        rejected = join_external_outcome(session, {"session_id": "session-b", "verdict": "pass"})
        self.assertEqual(matched["status"], "matched")
        self.assertEqual(rejected["status"], "not_joined")
        self.assertIsNone(rejected["verdict"])

    def test_offline_cli_never_calls_agent_and_batch_keeps_parse_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            valid = root / "valid.jsonl"
            valid.write_text((FIXTURES / "codex_nested_call_result.jsonl").read_text(encoding="utf-8"), encoding="utf-8")
            invalid = root / "invalid.jsonl"
            invalid.write_text("not-json\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch("sys.argv", ["eval_session", "--dir", str(root), "--offline", "--format", "json"]), \
                    patch.object(eval_session, "call_agent", side_effect=AssertionError("offline analyzer call")), \
                    contextlib.redirect_stdout(stdout):
                eval_session.main()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["profile"], "process-v2")
            self.assertEqual(len(payload["sessions"]), 2)
            self.assertEqual(sorted(item["processing_status"] for item in payload["sessions"]), ["complete", "failed"])

    def test_legacy_profile_remains_additive_and_has_no_process_result(self):
        stdout = io.StringIO()
        with patch("sys.argv", ["eval_session", str(FIXTURES / "codex_nested_call_result.jsonl"), "--offline", "--profile", "legacy", "--format", "json"]), \
                contextlib.redirect_stdout(stdout):
            eval_session.main()
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["profile"], "legacy")
        self.assertIsNone(payload["process_v2"])
        self.assertIn("composite", payload)


if __name__ == "__main__":
    unittest.main()
