"""Existing explicit-format CLI behavior, independent of future Jev features."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import eval_session


class ExplicitFormatBaselineTest(unittest.TestCase):
    def test_explicit_json_runs_complete_local_pipeline_without_analyzer(self):
        records = [
            {"type": "session_meta", "payload": {"id": "baseline-local", "model": "fixture-model"}},
            {"type": "response_item", "payload": {"role": "user", "content": [{"type": "input_text", "text": "Summarize the recorded observation."}]}},
            {"type": "response_item", "payload": {"role": "assistant", "content": [{"type": "output_text", "text": "The recorded observation is available."}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "synthetic.jsonl"
            source.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch("sys.argv", ["eval_session", str(source), "--format", "json"]), \
                    patch.object(eval_session, "call_agent", side_effect=AssertionError("Unexpected analyzer call")) as analyzer, \
                    contextlib.redirect_stdout(stdout):
                eval_session.main()
            payload = json.loads(stdout.getvalue())
            self.assertEqual(payload["session_id"], "baseline-local")
            self.assertEqual(payload["source"], "codex")
            self.assertEqual(payload["turn_count"], 1)
            self.assertIsNone(payload["agent_analysis"])
            analyzer.assert_not_called()
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()), ["synthetic.jsonl"])


if __name__ == "__main__":
    unittest.main()
