"""Focused checks for saved JSON report visualizations."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from lib.html_report import render_saved_html


ROOT = Path(__file__).resolve().parents[1]
AXES = ("SNR", "STATE", "CTX", "REACT", "DEPTH", "CONV", "TOOL")


def _legacy_session(session_id: str, offset: float = 0.0) -> dict:
    dimensions = {axis_id: 80.0 + offset for axis_id in AXES}
    return {
        "session_id": session_id,
        "source": "fixture",
        "model": "fixture-model",
        "turn_count": 3,
        "events": {"compactions": 0, "aborts": 0},
        "composite": 82.0 + offset,
        "grade": "B",
        "dimensions": dimensions,
        "stats": {"min": 80.0, "max": 90.0, "stddev": 2.0},
    }


def _process_session(session_id: str, *, missing: str | None = None) -> dict:
    axes = {}
    for index, axis_id in enumerate(AXES, 1):
        value = None if axis_id == missing else round(index / 10, 3)
        status = "unknown" if value is None else "observed"
        axes[axis_id] = {
            "axis_id": axis_id,
            "version": "process-v2.1",
            "metric": {
                "numerator": index if value is not None else None,
                "denominator": 10 if value is not None else None,
                "excluded_count": 0,
                "value": value,
                "coverage": 1.0 if value is not None else None,
                "applicability": "applicable" if value is not None else "unknown",
                "status": status,
                "reason": "fixture",
            },
            "observed_facts": {},
            "inference": {"quality_judgment": None},
            "evidence_refs": [],
            "notes": [],
        }
    return {
        "session_id": session_id,
        "source": "fixture",
        "model": "fixture-model",
        "turn_count": 3,
        "profile": "process-v2",
        "processing_status": "complete",
        "process_v2": {
            "profile": "process-v2",
            "version": "process-v2.1",
            "status": "complete",
            "axes": axes,
            "observed_facts": {},
            "inference": {"correctness_judgment": None},
            "external_outcome": {"status": "not_requested"},
            "coverage": {"axis_count": 7, "observed_axis_count": 7 - bool(missing)},
            "processing": {"mode": "offline"},
        },
    }


class SavedReportHtmlTest(unittest.TestCase):
    def test_legacy_batch_has_per_session_radars_and_comparison(self) -> None:
        payload = {
            "report_kind": "batch",
            "target_kind": "sessions_dir",
            "sessions": [_legacy_session("legacy-a"), _legacy_session("legacy-b", 4.0)],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "legacy.json"
            source.write_text(json.dumps(payload), encoding="utf-8")
            rendered = render_saved_html(str(source), input_kind="batch")

        self.assertEqual(rendered.count('class="legacy-mini-radar"'), 2)
        self.assertIn('class="batch-comparison"', rendered)
        self.assertIn("legacy heuristic composite", rendered)
        self.assertIn('viewBox="-40 -35 480 480"', rendered)
        self.assertIn(".legacy-mini-radar .grid-line", rendered)
        self.assertIn(".legacy-mini-radar .data-area", rendered)
        self.assertIn("fill: rgba(79, 195, 247, .15)", rendered)

    def test_process_single_uses_observed_ratio_polar_view_only_when_complete(self) -> None:
        complete = _process_session("process-complete")
        missing = _process_session("process-missing", missing="CTX")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete_path = root / "complete.json"
            missing_path = root / "missing.json"
            complete_path.write_text(json.dumps(complete), encoding="utf-8")
            missing_path.write_text(json.dumps(missing), encoding="utf-8")
            complete_html = render_saved_html(str(complete_path), input_kind="single")
            missing_html = render_saved_html(str(missing_path), input_kind="single")

        self.assertIn('class="process-polar"', complete_html)
        self.assertIn("not a calibrated quality score", complete_html)
        self.assertIn('class="process-bars"', complete_html)
        self.assertNotIn('class="process-polar"', missing_html)
        self.assertIn('data-axis="CTX" data-status="unknown"', missing_html)
        self.assertIn(">null<", missing_html)
        self.assertNotIn("width=\"0.0\"", missing_html)

    def test_process_batch_and_directory_cli_render_saved_data(self) -> None:
        first = _process_session("process-a")
        second = _process_session("process-b", missing="DEPTH")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_dir = root / "saved-sessions"
            input_dir.mkdir()
            (input_dir / "a.json").write_text(json.dumps(first), encoding="utf-8")
            (input_dir / "b.json").write_text(json.dumps(second), encoding="utf-8")
            output = root / "rendered.html"
            run = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "render_saved_report.py"),
                    "--input",
                    str(input_dir),
                    "--output",
                    str(output),
                    "--input-kind",
                    "directory",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            rendered = output.read_text(encoding="utf-8")

        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("HTML report saved to", run.stdout)
        self.assertIn('class="batch-heatmap"', rendered)
        self.assertIn('class="process-coverage"', rendered)
        self.assertIn('data-axis="DEPTH"', rendered)
        self.assertIn('data-status="unknown"', rendered)


if __name__ == "__main__":
    unittest.main()
