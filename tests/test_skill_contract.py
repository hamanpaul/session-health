"""Skill package policy checks."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillContractTest(unittest.TestCase):
    def test_skill_keeps_interactive_analysis_on_trigger_agent(self):
        text = (ROOT / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("current triggering agent", text)
        self.assertIn("--analysis-stdin", text)
        self.assertIn("Do not create", text)
        self.assertIn("only mode", text)

    def test_openai_metadata_is_present(self):
        text = (ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Session Health"', text)
        self.assertIn("allow_implicit_invocation: true", text)


if __name__ == "__main__":
    unittest.main()
