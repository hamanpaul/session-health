"""RED regression for the accepted Jev semantic CLI entry point."""

from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class JevSemanticCliRegressionTest(unittest.TestCase):
    def test_help_exposes_the_explicit_jev_mode(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "eval_session.py"), "--help"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--jev", result.stdout)


if __name__ == "__main__":
    unittest.main()
