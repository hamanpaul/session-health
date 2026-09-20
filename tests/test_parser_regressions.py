"""RED regressions for portable offline parser inputs."""

from pathlib import Path
import unittest

from lib.parser_codex import parse_codex_session
from lib.parser_copilot import parse_copilot_session


FIXTURES = Path(__file__).parent / "fixtures"


class OfflineParserRegressionTest(unittest.TestCase):
    def test_codex_nested_call_results_are_paired_and_unknown_event_is_retained(self):
        session = parse_codex_session(FIXTURES / "codex_nested_call_result.jsonl")

        self.assertEqual(session.id, "codex-nested-fixture")
        self.assertEqual(session.turn_count, 1)
        turn = session.turns[0]
        self.assertEqual(
            [(call.call_id, call.name, call.output) for call in turn.tool_calls],
            [
                ("call-nested-1", "exec_command", "nested\n"),
                ("call-unsupported-1", "future_tool", "unsupported tool"),
            ],
        )
        self.assertEqual(turn.tool_calls[0].arguments, {"cmd": "printf nested"})
        self.assertEqual(turn.tool_calls[1].arguments, {"opaque": True})
        self.assertIn(
            {"type": "future_event", "status": "unknown", "value": "preserve"},
            turn.events,
        )

    def test_copilot_json_string_arguments_keep_failed_and_unsupported_calls(self):
        session = parse_copilot_session(FIXTURES / "copilot_json_string_arguments.jsonl")

        self.assertEqual(session.id, "copilot-json-args")
        self.assertEqual(session.turn_count, 1)
        turn = session.turns[0]
        self.assertEqual(
            [(call.call_id, call.name, call.success, call.output) for call in turn.tool_calls],
            [
                ("tool-bash-1", "bash", False, "permission denied"),
                ("tool-unsupported-1", "future.tool", False, "unsupported tool"),
            ],
        )
        self.assertEqual(turn.tool_calls[0].arguments, {"command": "printf copilot"})
        self.assertEqual(turn.tool_calls[1].arguments, {"opaque": True})


if __name__ == "__main__":
    unittest.main()
