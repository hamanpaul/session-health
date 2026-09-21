"""Interactive trigger-agent no-file protocol tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lib.trigger_analysis import parse_trigger_analysis


ROOT = Path(__file__).resolve().parents[1]


def _session(path: Path) -> None:
    rows = [
        {"type": "session_meta", "payload": {"id": "trigger-fixture", "model": "fixture"}},
        {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "inspect"}]}},
        {"type": "response_item", "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "done"}]}},
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class TriggerAgentContractTest(unittest.TestCase):
    def test_context_step_emits_bounded_contract_without_handoff_file(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            _session(source)
            result = subprocess.run(
                [sys.executable, str(ROOT / "eval_session.py"), str(source), "--analysis-context", "--analysis-origin", "trigger-agent"],
                cwd=directory,
                capture_output=True,
                text=True,
                check=False,
            )
            files = sorted(item.name for item in Path(directory).iterdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["schema"], "session-health.trigger-context")
        self.assertEqual(payload["task_profile"]["axes"].keys(), {"SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL"})
        self.assertEqual(files, ["session.jsonl"])

    def test_stdin_step_records_trigger_origin_model_and_usage(self):
        analysis = {
            "observations": [{"text": "Evidence is bounded.", "evidence_refs": [], "counterevidence_refs": []}],
            "hypotheses": [],
            "claims": [],
            "recommendations": [{"text": "Keep the deterministic stage.", "evidence_refs": [], "counterevidence_refs": []}],
            "actual_model": "gpt-5.6-luna",
            "provider": "openai",
            "native_usage": {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18},
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "session.jsonl"
            _session(source)
            result = subprocess.run(
                [sys.executable, str(ROOT / "eval_session.py"), str(source), "--analysis-stdin", "--analysis-origin", "trigger-agent", "--format", "json"],
                cwd=directory,
                input=json.dumps(analysis),
                capture_output=True,
                text=True,
                check=False,
            )
            files = sorted(item.name for item in Path(directory).iterdir())
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["analysis_status"], "completed")
        self.assertEqual(payload["agent_analysis"]["analysis_origin"], "trigger_agent")
        self.assertEqual(payload["agent_analysis"]["actual_model"], "gpt-5.6-luna")
        self.assertEqual(payload["agent_analysis"]["native_usage"]["total_tokens"], 18)
        self.assertEqual(files, ["session.jsonl"])

    def test_unknown_reference_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown evidence reference"):
            parse_trigger_analysis(
                json.dumps({"observations": [{"text": "x", "evidence_refs": ["missing"]}]}),
                allowed_refs={"known"},
            )


if __name__ == "__main__":
    unittest.main()
